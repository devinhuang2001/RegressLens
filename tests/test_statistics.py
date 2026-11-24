from __future__ import annotations

from regresslens.model import Config, Experiment, Metric, Scenario
from regresslens.statistics import compare


def make_runs(values, prefix, digest="same"):
    return [
        Experiment(id=f"{prefix}-{i}", commit=prefix, scenario="s", pair=i,
                   seed=i, comparison="a-b", spec_digest=digest, started_at="0", ended_at="1",
                   environment={"fingerprint": "same"}, metrics={"throughput": value},
                   distributions={}, artifacts={})
        for i, value in enumerate(values)
    ]


def test_throughput_direction_and_missing_values():
    metric = Metric(name="throughput", direction="higher", threshold=0.1)
    config = Config(model="m", model_revision="r", tokenizer="t", tokenizer_revision="r",
                    scenarios=[Scenario(id="s")], gate_scenario="s", gate_metric=metric.name, metrics=[metric])
    baseline = make_runs([100] * 5, "baseline")
    assert compare(baseline, make_runs([82] * 5, "candidate"), "s", metric, config).decision == "FAIL"
    assert compare(baseline, make_runs([109] * 5, "candidate"), "s", metric, config).decision == "PASS"
    assert compare(baseline, make_runs([82] * 4, "candidate"), "s", metric, config).decision == "INCONCLUSIVE"
    assert compare(baseline, make_runs([82] * 5, "candidate", "different"), "s", metric, config).decision == "INVALID"
    duplicate = baseline + [baseline[0]]
    assert compare(duplicate, make_runs([82] * 5, "candidate"), "s", metric, config).decision == "INVALID"
