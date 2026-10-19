from studio.core.batch_runner import BatchRunner
from studio.core.batch_store import BatchStore, EpisodeState


def _paused_runner(tmp_path, episode_state=EpisodeState.PENDING.value):
    store = BatchStore(tmp_path / "paused-jobs.db")
    batch = store.create_batch(str(tmp_path / "season"))
    episodes = store.add_episodes(batch["id"], [{
        "source_path": str(tmp_path / "season" / "01.mkv"),
        "filename": "01.mkv",
        "order_index": 1,
        "state": episode_state,
    }])
    store.update_batch(batch["id"], status="PAUSED")
    return store, BatchRunner(store=store), batch["id"], episodes[0]["id"]


def test_paused_startup_and_all_worker_loops_do_not_claim_episodes(tmp_path):
    store, runner, batch_id, episode_id = _paused_runner(tmp_path)
    store.add_episodes(batch_id, [{
        "source_path": str(tmp_path / "season" / "02.mkv"),
        "filename": "02.mkv",
        "order_index": 2,
        "state": EpisodeState.NEEDS_REVIEW.value,
    }])
    review_id = next(ep["id"] for ep in store.get_episodes(batch_id) if ep["filename"] == "02.mkv")
    claimed = []
    uploads = []
    runner._process_single_episode = lambda episode: claimed.append(episode["id"])
    runner.upload_episode = lambda episode_id, **kwargs: uploads.append(episode_id)

    # Simulate worker entry points being woken without an explicit start/resume.
    runner._sequential_worker_loop()
    runner._pipelined_encoder_loop()
    runner._pipelined_upload_loop()

    assert not claimed
    assert not uploads
    assert store.get_batch(batch_id)["status"] == "PAUSED"
    assert store.get_episode(episode_id)["state"] == EpisodeState.PENDING.value
    assert store.get_episode(review_id)["state"] == EpisodeState.NEEDS_REVIEW.value
    assert not runner.is_running
    assert not runner.is_uploading


def test_interrupted_active_batch_recovers_paused_without_auto_resume(tmp_path):
    store = BatchStore(tmp_path / "interrupted-jobs.db")
    batch = store.create_batch(str(tmp_path / "season"))
    store.add_episodes(batch["id"], [{
        "source_path": str(tmp_path / "season" / "01.mkv"),
        "filename": "01.mkv",
        "order_index": 1,
        "state": EpisodeState.PENDING.value,
    }])

    store.update_batch(batch["id"], status="ACTIVE")
    runner = BatchRunner(store=store)

    assert store.get_batch(batch["id"])["status"] == "PAUSED"
    assert not runner.is_running
    assert not runner.is_uploading


def test_startup_pauses_every_interrupted_batch_and_never_launches_processes(tmp_path, monkeypatch):
    store = BatchStore(tmp_path / "multiple-interrupted.db")
    batches = []
    episodes = []
    for season in ("Season 01", "Season 02"):
        batch = store.create_batch(str(tmp_path / season))
        episode = store.add_episodes(batch["id"], [{
            "source_path": str(tmp_path / season / "01.mkv"),
            "filename": "01.mkv",
            "order_index": 1,
            "state": EpisodeState.ENCODING.value,
            "work_dir": str(tmp_path / season / "partial"),
            "progress": 43,
        }])[0]
        store.update_batch(batch["id"], status="ACTIVE")
        batches.append(batch["id"])
        episodes.append(episode["id"])

    process_attempts = []

    def forbidden_process(*args, **kwargs):
        process_attempts.append(args)
        raise AssertionError("startup attempted to launch a child process")

    monkeypatch.setattr("studio.core.batch_runner.popen_headless", forbidden_process)
    monkeypatch.setattr("studio.core.batch_runner.run_headless", forbidden_process)
    runner = BatchRunner(store=store)

    assert [store.get_batch(batch_id)["status"] for batch_id in batches] == ["PAUSED", "PAUSED"]
    assert [store.get_episode(episode_id)["state"] for episode_id in episodes] == [
        EpisodeState.PENDING.value,
        EpisodeState.PENDING.value,
    ]
    assert [store.get_episode(episode_id)["progress"] for episode_id in episodes] == [43, 43]
    assert not process_attempts
    assert not runner.is_running
    assert not runner.is_uploading


def test_completed_jobs_are_preserved_at_startup_without_process_launch(tmp_path, monkeypatch):
    store = BatchStore(tmp_path / "completed-startup.db")
    batch = store.create_batch(str(tmp_path / "season"))
    episodes = store.add_episodes(batch["id"], [
        {
            "source_path": str(tmp_path / "season" / "ready.mkv"),
            "filename": "ready.mkv",
            "order_index": 1,
            "state": EpisodeState.READY.value,
        },
        {
            "source_path": str(tmp_path / "season" / "packaged.mkv"),
            "filename": "packaged.mkv",
            "order_index": 2,
            "state": EpisodeState.READY_TO_UPLOAD.value,
        },
    ])
    store.update_batch(batch["id"], status="ACTIVE")
    monkeypatch.setattr(
        "studio.core.batch_runner.popen_headless",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected process launch")),
    )
    monkeypatch.setattr(
        "studio.core.batch_runner.run_headless",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected process launch")),
    )

    runner = BatchRunner(store=store)

    assert store.get_batch(batch["id"])["status"] == "PAUSED"
    assert [store.get_episode(ep["id"])["state"] for ep in episodes] == [
        EpisodeState.READY.value,
        EpisodeState.READY_TO_UPLOAD.value,
    ]
    assert not runner.is_running
    assert not runner.is_uploading


def test_failed_and_completed_batches_cannot_be_restarted(tmp_path):
    for status in ("FAILED", "COMPLETED", "COMPLETED_LOCAL", "DELETED"):
        store, runner, batch_id, _ = _paused_runner(tmp_path / status)
        store.update_batch(batch_id, status=status)

        for start in (runner.start_batch, runner.start_automatic_pipeline):
            try:
                start(batch_id)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{status} batch was allowed to start")

        assert not runner.is_running
        assert not runner.is_uploading


def test_retry_preserves_failed_attempt_and_failure_reason(tmp_path):
    store, runner, batch_id, episode_id = _paused_runner(tmp_path, EpisodeState.FAILED.value)
    settings = {
        "processing_history": {
            "attempt_count": 2,
            "retry_count": 1,
            "attempts": [
                {"attempt_number": 1, "success": False},
                {"attempt_number": 2, "success": False, "attempt_duration_seconds": 463.83},
            ],
            "successful": None,
        }
    }
    failure = "interrupted during startup acceptance"
    store.update_episode(episode_id, settings_json=settings, error=failure)

    runner.retry_episode(episode_id)
    reset = store.get_episode(episode_id)
    history = reset["settings"]["processing_history"]

    assert reset["state"] == EpisodeState.PENDING.value
    assert reset["error"] is None
    assert history["attempt_count"] == 2
    assert history["retry_count"] == 1
    assert history["attempts"][1]["success"] is False
    assert history["attempts"][1]["attempt_duration_seconds"] == 463.83
    assert history["attempts"][1]["failure_reason"] == failure
    assert history["successful"] is None
    assert store.get_batch(batch_id)["status"] == "PAUSED"
    assert not runner.is_running


def test_single_episode_start_targets_selected_row_without_changing_neighbors(tmp_path, monkeypatch):
    store, runner, batch_id, first_id = _paused_runner(tmp_path)
    store.add_episodes(batch_id, [{
        "source_path": str(tmp_path / "season" / "02.mkv"),
        "filename": "02.mkv",
        "order_index": 2,
        "state": EpisodeState.PENDING.value,
    }])
    second = next(ep for ep in store.get_episodes(batch_id) if ep["filename"] == "02.mkv")
    starts = []

    def fake_start(target_batch, *, single_episode_id=None):
        runner._single_episode_target_id = single_episode_id
        runner._worker_stop_event.clear()
        starts.append((target_batch, single_episode_id))
        return {"batch_id": target_batch}

    monkeypatch.setattr(runner, "start_batch", fake_start)
    result = runner.start_episode(second["id"])
    assert result["batch_id"] == batch_id
    assert starts == [(batch_id, second["id"])]
    assert runner._stop_after_current_requested is True
    assert runner._single_episode_target_id == second["id"]
    assert store.get_episode(first_id)["state"] == EpisodeState.PENDING.value
    assert store.get_episode(second["id"])["state"] == EpisodeState.PENDING.value

    claimed = []
    monkeypatch.setattr(runner, "_process_single_episode", lambda ep: claimed.append(ep["id"]) or True)
    runner._active_batch_id = batch_id
    runner._work_authorized = True
    runner._run_authorization = (runner._studio_session_id, batch_id, "test-grant")
    store.update_batch(batch_id, status="ACTIVE")
    runner._sequential_worker_loop()
    assert claimed == [second["id"]]
    assert store.get_batch(batch_id)["status"] == "PAUSED"
    assert store.get_episode(first_id)["state"] == EpisodeState.PENDING.value
    assert store.get_episode(second["id"])["state"] == EpisodeState.PENDING.value


def test_font_audit_refresh_decodes_saved_json_and_releases_resolved_review(tmp_path, monkeypatch):
    import studio.core.batch_runner as runner_module

    store = BatchStore(tmp_path / "font-refresh.db")
    batch = store.create_batch(str(tmp_path / "season"))
    episode = store.add_episodes(batch["id"], [{
        "source_path": str(tmp_path / "season" / "01.mkv"),
        "filename": "01.mkv",
        "order_index": 1,
        "state": EpisodeState.NEEDS_REVIEW.value,
        "error": "خطوط مفقودة (1/1): Poppins",
        "missing_fonts": ["Poppins"],
        "font_audit": {
            "total_fonts": 1,
            "available_count": 0,
            "missing_count": 1,
            "missing_font_names": ["Poppins"],
            "fonts": [{"name": "Poppins", "status": "missing"}],
        },
    }])[0]
    runner = BatchRunner(store=store)

    def resolve(audit):
        refreshed = dict(audit)
        refreshed["fonts"] = [{"name": "Poppins", "status": "font_library"}]
        refreshed.update({"available_count": 1, "missing_count": 0, "missing_font_names": [], "all_fonts_present": True})
        return refreshed

    monkeypatch.setattr(runner_module, "apply_font_library_to_audit", resolve)
    result = runner.refresh_font_library_audits()
    updated = store.get_episode(episode["id"])

    assert result == {"updated": 1, "ready": 1, "needs_review": 0}
    assert updated["state"] == EpisodeState.PENDING.value
    assert updated["missing_fonts_list"] == []
    assert updated["font_audit"]["fonts"][0]["status"] == "font_library"
    assert updated["error"] is None
