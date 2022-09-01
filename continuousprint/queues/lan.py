import uuid
from bisect import bisect_left
from peerprint.lan_queue import LANPrintQueue, ChangeType
from ..storage.lan import LANJobView
from ..storage.database import JobView
from pathlib import Path
from .abstract import AbstractEditableQueue, QueueData, Strategy
import dataclasses


class LANQueue(AbstractEditableQueue):
    def __init__(
        self,
        ns,
        addr,
        logger,
        strategy: Strategy,
        update_cb,
        fileshare,
        profile,
        path_on_disk_fn,
    ):
        super().__init__()
        self._logger = logger
        self._profile = profile
        self.strategy = strategy
        self.ns = ns
        self.addr = addr
        self.lan = None
        self.update_cb = update_cb
        self._fileshare = fileshare
        self._path_on_disk = path_on_disk_fn
        self.lan = LANPrintQueue(self.ns, self.addr, self._on_update, self._logger)

    # ---------- LAN queue methods ---------

    def is_ready(self) -> bool:
        return self.lan.q.is_ready()

    def connect(self):
        self.lan.connect()

    def _compare_peer(self, prev, nxt):
        if prev is None:
            return True
        for k in ("status", "run"):
            if prev.get(k) != nxt.get(k):
                return True
        return False

    def _compare_job(self, prev, nxt):
        return True  # Always trigger callback - TODO make this more sophisticated

    def _on_update(self, changetype, prev, nxt):
        if changetype == ChangeType.PEER and not self._compare_peer(prev, nxt):
            return
        elif changetype == ChangeType.JOB and not self._compare_job(prev, nxt):
            return
        self.update_cb(self)

    def destroy(self):
        self.lan.destroy()

    def update_peer_state(self, name, status, run, profile):
        if self.lan is not None and self.lan.q is not None:
            self.lan.q.syncPeer(
                dict(
                    active_set=self._active_set(),
                    name=name,
                    status=status,
                    run=run,
                    profile=profile,
                    fs_addr=f"{self._fileshare.host}:{self._fileshare.port}",
                )
            )

    def set_job(self, hash_: str, manifest: dict):
        return self.lan.q.setJob(hash_, manifest)

    def resolve_set(self, peer, hash_, path) -> str:
        # Get fileshare address from the peer
        peerstate = self._get_peers().get(peer)
        if peerstate is None:
            raise Exception(
                "Cannot resolve set {path} within job hash {hash_}; peer state is None"
            )

        # fetch unpacked job from fileshare (may be cached) and return the real path
        gjob_dirpath = self._fileshare.fetch(peerstate["fs_addr"], hash_, unpack=True)
        return str(Path(gjob_dirpath) / path)

    # -------- Wrappers around LANQueue to add/remove metadata ------

    def _annotate_job(self, peer_and_manifest, acquired_by):
        (peer, manifest) = peer_and_manifest
        m = dict(**manifest)
        m["peer_"] = peer
        m["acquired"] = True if acquired_by is not None else False
        m["acquired_by_"] = acquired_by
        return m

    def _normalize_job(self, data):
        del m["peer_"]
        del m["acquired_by_"]

    def _get_jobs(self) -> list:
        joblocks = self.lan.q.getLocks()
        jobs = []
        for (jid, v) in self.lan.q.getJobs():
            jobs.append(self._annotate_job(v, joblocks.get(jid)))
        return jobs

    def _get_job(self, jid) -> dict:
        j = self.lan.q.getJob(jid)
        joblocks = self.lan.q.getLocks()
        if j is not None:
            return self._annotate_job(j, joblocks.get(jid))

    def _get_peers(self) -> list:
        result = {}
        # Locks are given by job:peer, so reverse this
        peerlocks = dict([(v, k) for k, v in self.lan.q.getLocks().items()])
        for k, v in self.lan.q.getPeers().items():
            result[k] = dict(**v, acquired=peerlocks.get(k, []))
        return result

    # --------- begin AbstractQueue --------

    def _peek(self):
        if self.lan is None or self.lan.q is None:
            return (None, None)
        for data in self._get_jobs():
            acq = data.get("acquired_by_")
            if acq is not None and acq != self.addr:
                continue  # Acquired by somebody else, so don't consider for scheduling
            job = LANJobView(data, self)
            s = job.next_set(self._profile)
            if s is not None:
                return (job, s)
        return (None, None)

    def acquire(self) -> bool:
        if self.lan is None or self.lan.q is None:
            return False
        (job, s) = self._peek()
        if job is not None and s is not None:
            if self.lan.q.acquireJob(job.id):
                self._logger.debug(f"acquire() candidate:\n{job}\n{s}")
                self.job = self.get_job_view(job.id)  # Re-fetch to get assigned state
                self.set = s
                self._logger.debug("acquire() success")
                return True
            else:
                self._logger.debug("acquire() failed")
                return False
        else:
            return False

    def release(self) -> None:
        if self.job is not None:
            self.lan.q.releaseJob(self.job.id)
            self.job = None
            self.set = None

    def decrement(self) -> None:
        if self.job is not None:
            next_set = self.set.decrement(self._profile)
            if next_set:
                self._logger.debug("Still has work, going for next set")
                self.set = next_set
                return True
            else:
                self._logger.debug("No more work; releasing")
                self.release()
                return False

    def _active_set(self):
        assigned = self.get_set()
        if assigned is not None:
            return assigned.id
        return None

    def as_dict(self) -> dict:
        jobs = []
        peers = {}
        if self.lan.q is not None:
            jobs = self._get_jobs()
            peers = self._get_peers()
            for j in jobs:
                j["queue"] = self.ns

        return dataclasses.asdict(
            QueueData(
                name=self.lan.ns,
                addr=self.addr,
                strategy=self.strategy.name,
                jobs=jobs,
                peers=peers,
                active_set=self._active_set(),
            )
        )

    def reset_jobs(self, job_ids) -> dict:
        for jid in job_ids:
            j = self._get_job(jid)
            if j is None:
                continue

            j["remaining"] = j["count"]
            for s in j.get("sets", []):
                s["remaining"] = s["count"]
            self.lan.q.setJob(jid, j, addr=j["peer_"])

    def remove_jobs(self, job_ids) -> dict:
        n = 0
        for jid in job_ids:
            if self.lan.q.removeJob(jid) is not None:
                n += 1
        return dict(jobs_deleted=n)

    # --------- end AbstractQueue ------

    # --------- AbstractEditableQueue implementation ------

    def get_job_view(self, job_id):
        return LANJobView(self._get_job(job_id), self)

    def import_job_from_view(self, j):
        err = self._validate_job(j)
        if err is not None:
            self._logger.warning(err)
            return Exception(err)
        filepaths = dict([(s.path, self._path_on_disk(s.path, s.sd)) for s in j.sets])
        manifest = j.as_dict()
        if manifest.get("created") is None:
            manifest["created"] = int(time.time())
        # Note: postJob strips fields from manifest in-place
        manifest["hash"] = self._fileshare.post(manifest, filepaths)
        if manifest.get("id") is None:
            manifest["id"] = self._gen_uuid()
        self.lan.q.setJob(manifest["id"], manifest)
        return manifest["id"]

    def mv_job(self, job_id, after_id):
        self.lan.q.jobs.mv(job_id, after_id)

    def _path_exists(self, fullpath):
        return Path(fullpath).exists()

    def _validate_job(self, j: JobView) -> str:
        peer_profiles = set(
            [
                p.get("profile", dict()).get("name", "UNKNOWN")
                for p in self._get_peers().values()
            ]
        )

        for s in j.sets:
            sprof = set(s.profiles())
            # All sets in the job *must* have an assigned profile
            if len(sprof) == 0:
                return f"validation for job {j.name} failed - set {s.path} has no assigned profile"

            # At least one printer in the queue must have a compatible proile
            if len(peer_profiles.intersection(sprof)) == 0:
                return f"validation for job {j.name} failed - no match for set {s.path} with profiles {sprof} (connected printer profiles: {peer_profiles})"

            # All set paths must resolve to actual files
            fullpath = self._path_on_disk(s.path, s.sd)
            if fullpath is None or not self._path_exists(fullpath):
                return f"validation for job {j.name} failed - file not found at {s.path} (is it stored on disk and not SD?)"

    def _gen_uuid(self) -> str:
        while True:
            result = uuid.uuid4()
            if not self.lan.q.hasJob(result):
                return str(result)

    def edit_job(self, job_id, data) -> bool:
        # For lan queues, "editing" a job is basically resubmission of the whole thing.
        # This is because the backing .gjob format is a single file containing the full manifest.
        j = self.get_job_view(job_id)
        for (k, v) in data.items():
            if k in ("id", "peer_", "queue"):
                continue
            setattr(j, k, v)
        self.import_job_from_view(j)
