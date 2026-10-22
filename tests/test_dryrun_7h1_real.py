"""7H1本番モデル・ドライランのフェイルクローズ不変条件。"""
from datetime import datetime
from pathlib import Path

import pytest

from scripts import dryrun_7h1_real as runner


def test_preflight_rejects_missing_production_models(tmp_path, monkeypatch):
    """旧lgbm.pklが存在しても7H1の4モデルを置き換えない。"""
    (tmp_path / "data" / "models").mkdir(parents=True)
    (tmp_path / "data" / "models" / "lgbm.pkl").write_bytes(b"legacy")
    monkeypatch.setattr(runner, "ROOT", Path(tmp_path))
    with pytest.raises(RuntimeError, match="lgbm_wt_favbust"):
        runner.preflight()


def test_historical_prediction_requires_vintage():
    """当日以外の過去日には全期間モデルを使用しない。"""
    with pytest.raises(RuntimeError, match="リーク"):
        runner.run("2020-01-01", "小田原")


def test_production_model_manifest_is_exact():
    assert runner.MODEL_NAMES == (
        "lgbm_wt_eval", "lgbm_wt_win", "lgbm_wt_bad", "lgbm_wt_favbust"
    )

def test_main_runs_fail_closed_when_models_are_absent(tmp_path, monkeypatch, capsys):
    """CLIを実際に通して、推論していないことをJSONで報告する。"""
    import json
    import sys
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    monkeypatch.setattr(runner, "ROOT", Path(tmp_path))
    monkeypatch.setattr(sys, "argv", [
        "dryrun_7h1_real.py", "--date", today, "--venue", "小田原"
    ])
    assert runner.main() == 2
    report = json.loads(capsys.readouterr().err)
    assert report["status"] == "実モデル推論未実施"
    assert "lgbm_wt_favbust" in report["error"]
