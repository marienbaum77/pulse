import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend" / "scripts" / "eval_editorial_model.py"


def run_script(arguments, timeout=120):
    env = {**os.environ, "PYTHONUTF8": "1"}
    return subprocess.run([sys.executable, *arguments], cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False)


def test_synthetic_eval_writes_metrics_without_figures(tmp_path):
    result = run_script([str(SCRIPT), "--synthetic", "--out", str(tmp_path), "--no-figures"])
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert report["warning"] == "СИНТЕТИЧЕСКИЙ демо-прогон — не реальная оценка качества"
    assert report["parameters"]["bootstrap_rounds"] == 1000
    assert report["n_train"] > 0 and report["n_test"] > 0
    for method in ("proposed", "nws", "topic"):
        assert report["methods"][method]["pr_auc"] >= 0
        assert len(report["methods"][method]["ci95"]["pr_auc"]) == 2
    assert report["methods"]["llm"]["status"] == "н/д"
    assert report["methods"]["proposed"]["latency_median_ms"] >= 0
    assert report["methods"]["proposed"]["state_bytes"] <= 20_000


def test_eval_without_matplotlib_still_prints_table_and_metrics(tmp_path):
    program = """
import builtins
import runpy
import sys
real_import = builtins.__import__
def without_matplotlib(name, *args, **kwargs):
    if name == "matplotlib" or name.startswith("matplotlib."):
        raise ImportError("matplotlib disabled for test")
    return real_import(name, *args, **kwargs)
builtins.__import__ = without_matplotlib
script = sys.argv[1]
sys.argv = [script, *sys.argv[2:]]
runpy.run_path(script, run_name="__main__")
"""
    result = run_script(["-c", program, str(SCRIPT), "--synthetic", "--out", str(tmp_path)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Графики пропущены" in result.stdout
    assert "pip install -r backend/requirements-dev.txt" in result.stdout
    assert "PR-AUC" in result.stdout
    assert (tmp_path / "metrics.json").is_file()
