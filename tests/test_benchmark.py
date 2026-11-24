from __future__ import annotations

import pytest

from regresslens.benchmark import guidellm_spec, normalize, promote, prometheus
from regresslens.model import Config, Scenario, Service


def distribution(mean, p95, count=8):
    return {"successful": {"mean": mean, "percentiles": {"p95": p95}, "count": count}}


def test_guidellm_normalization_preserves_units_counts_and_missing():
    metrics, distributions = normalize({"benchmarks": [{"metrics": {
        "time_to_first_token_ms": distribution(8, 10),
        "time_per_output_token_ms": distribution(1, 2),
        "request_latency": distribution(0.03, 0.04),
        "output_tokens_per_second": distribution(500, 600),
        "inter_token_latency_ms": distribution(0, 0, count=0),
        "request_totals": {"successful": 8, "incomplete": 1, "errored": 1, "total": 10},
        "slo_attainment": 0.8,
    }}]})
    assert metrics == {"ttft_ms_p95": 10, "tpot_ms_p95": 2, "e2e_ms_p95": 40,
                       "output_tokens_per_second": 500, "requests_total": 10,
                       "requests_successful": 8, "error_rate": 0.2, "slo_attainment": 0.8}
    assert distributions["e2e_ms"]["count"] == 8
    assert distributions["e2e_ms"]["mean"] == 30
    assert "itl_ms" not in distributions


def test_prometheus_window_ignores_counter_resets():
    before = prometheus('vllm:request_queue_time_seconds_sum{model_name="x"} 1.0\n'
                        'vllm:request_queue_time_seconds_count{model_name="x"} 5\n'
                        'vllm:num_preemptions_total{model_name="x"} 2\n')
    after = prometheus('vllm:request_queue_time_seconds_sum{model_name="x"} 2.0\n'
                       'vllm:request_queue_time_seconds_count{model_name="x"} 10\n'
                       'vllm:num_preemptions_total{model_name="x"} 1\n'
                       'vllm:kv_cache_usage_perc{model_name="x"} 0.5\n')
    assert promote(before, after) == {"queue_ms_mean": 200, "kv_cache_usage_snapshot": 0.5}


def test_configuration_rejects_saved_secret_arguments():
    with pytest.raises(ValueError, match="Credentials"):
        Service(command=["vllm", "serve", "--api-key=placeholder"])


def test_actual_guidellm_081_scenario_schema():
    module = pytest.importorskip("guidellm.schemas.benchmark.entrypoints")
    scenario = Scenario(id="benchmark", concurrency=4)
    config = Config(model="test", tokenizer="test", model_revision="0" * 40,
                    tokenizer_revision="1" * 40, gate_scenario=scenario.id,
                    slo={"ttft_ms": 1000}, scenarios=[scenario])
    payload = guidellm_spec(config, scenario, "http://127.0.0.1:8000", 42, 64, "out.json")
    validated = module.BenchmarkScenario.model_validate(payload)
    assert validated.spec.profile.streams == [4]
    assert validated.spec.data_loader.num_workers == 1
    assert validated.spec.data_loader.prefetch_factor == 16
    assert validated.spec.constraints[0].count == 64
    assert validated.spec.metrics.slo.ttft_ms == 1000
