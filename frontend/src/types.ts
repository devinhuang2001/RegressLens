import type { Profile } from "./Profiling";

export type Gate = {
  scenario: string;
  metric: string;
  baseline: number | null;
  candidate: number | null;
  delta: number | null;
  interval: [number, number] | null;
  threshold: number;
  confidence: number;
  pairs: number;
  decision: string;
  warnings?: string[];
  baseline_ids: string[];
  candidate_ids: string[];
};
export type Run = {
  id: string;
  commit: string;
  scenario: string;
  pair: number;
  environment: {
    gpu: { name: string }[];
    runtime: Record<string, string | null>;
    fingerprint: string;
  };
  metrics: Record<string, number>;
  artifacts: Record<string, string>;
  distributions: Record<string, Record<string, number>>;
  warnings: string[];
};
export type Commit = { sha: string; subject?: string; status: string; reason?: string; gate?: Gate; at?: string };
export type Evidence = {
  metric: string; scenario: string; baseline: number; candidate: number; relative_delta: number | null;
  baseline_ids: string[]; candidate_ids: string[];
};
export type Finding = {
  id: string; category: string; summary: string; verification: string;
  support: (Evidence | string)[]; against: Evidence[]; missing: string[]; files: string[]; next: string;
};
export type Investigation = {
  id: string; case_type: string; state: string; created_at: string; good: string; bad: string;
  gate: Gate | null; metrics: Gate[]; experiments: Record<string, Run>; commits: Commit[]; decisions: Commit[];
  config: {
    model: string; tokenizer: string; model_revision: string; tokenizer_revision: string; rounds: number; seed: number;
    gate_scenario: string; gate_metric: string;
    metrics?: { name: string; direction: 'higher' | 'lower'; threshold: number }[];
    scenarios: { id: string; prompt_tokens: number; output_tokens: number; concurrency: number; requests: number }[];
  };
  localization: { status: string; last_pass: string; first_fail: string; candidates: string[]; assumption: string } | null;
  findings: Finding[];
  verifications: { hypothesis: string; at: string; status: string; method: string; reason: string; commit?: string; evidence: Gate[] }[];
  source_diff?: string; changed_files?: string[]; source_diff_ref?: string;
  diagnostic_comparison?: { baseline: string; candidate: string };
  profiling?: Profile | null; profiling_warning?: string | null;
  error?: string; measurement_note: string; method: string; reproduction: string; artifact_root: string;
};
