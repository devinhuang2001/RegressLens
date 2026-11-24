from __future__ import annotations

import numpy as np

from .model import Config, Experiment, Metric, Result


def compare(
    baseline: list[Experiment], candidate: list[Experiment], scenario: str,
    metric: Metric, config: Config,
) -> Result:
    def by_pair(runs):
        return {run.pair: run for run in runs if run.scenario == scenario}

    left, right = by_pair(baseline), by_pair(candidate)
    result = Result(scenario=scenario, metric=metric.name, threshold=metric.threshold,
                    confidence=config.confidence, decision="INCONCLUSIVE")
    if (len(left) != sum(run.scenario == scenario for run in baseline)
            or len(right) != sum(run.scenario == scenario for run in candidate)):
        result.decision = "INVALID"
        result.warnings = ["Duplicate experiment pair"]
        return result
    pairs = [(left[i], right[i]) for i in sorted(left.keys() & right.keys())]
    result.baseline_ids = [a.id for a, _ in pairs]
    result.candidate_ids = [b.id for _, b in pairs]
    observed = [run for pair in pairs for run in pair]
    comparable = {(run.environment.get("fingerprint"), run.spec_digest, run.comparison)
                  for run in observed}
    warnings = {warning for run in observed for warning in run.warnings}
    if len(comparable) > 1 or any(not run.environment.get("fingerprint") for run in observed):
        warnings.add("Execution environment, workload, or pairing provenance differs")
    if any(a.seed != b.seed for a, b in pairs):
        warnings.add("Paired seeds differ")
    if warnings:
        result.decision = "INVALID"
        result.warnings = sorted(warnings)
        return result
    values = [(a.metrics[metric.name], b.metrics[metric.name])
              for a, b in pairs if metric.name in a.metrics and metric.name in b.metrics]
    result.pairs = len(values)
    if len(values) < config.rounds or len(pairs) != len(values):
        result.warnings = ["Missing usable measurements for the fixed number of pairs"]
        return result
    a, b = np.asarray(values).T
    result.baseline, result.candidate = float(a.mean()), float(b.mean())
    if np.any(a <= 0) or np.any(b < 0):
        result.warnings = ["Relative effect requires positive baseline and nonnegative candidate values"]
        return result
    sign = 1 if metric.direction == "lower" else -1
    loss = sign * ((b - a) / a)
    result.delta = float(loss.mean())
    draws = np.random.default_rng(config.seed).choice(loss, size=(config.bootstrap_draws, len(loss)))
    alpha = (1 - config.confidence) / 2
    low, high = np.quantile(draws.mean(axis=1), [alpha, 1 - alpha])
    result.interval = float(low), float(high)
    if low >= metric.threshold:
        result.decision = "FAIL"
    elif high < metric.threshold:
        result.decision = "PASS"
    return result


def compare_all(baseline: list[Experiment], candidate: list[Experiment], config: Config):
    results = [compare(baseline, candidate, scenario.id, metric, config)
               for scenario in config.scenarios for metric in config.metrics]
    gate = next(row for row in results if (row.scenario, row.metric) ==
                (config.gate_scenario, config.gate_metric))
    return gate, results
