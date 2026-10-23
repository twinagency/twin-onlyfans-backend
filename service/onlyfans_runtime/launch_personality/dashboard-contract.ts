/** of-personality.v1. Backend-owned handoff; no dashboard files changed. */
export const PERSONALITY_VERSION = "of-personality.v1" as const;
export const CONFIGURATION_VERSION = "of-configuration.v3" as const;
export const CONFIGURATION_HEADER = "X-OnlyFans-Configuration-Version";
export type FactKey = "age" | "job" | "home_city" | "relationship" | "kids" | "family" | "tattoos" | "kinks" | "limits";
export type Author = "creator" | "agency" | "unknown" | "automation";
export type Conflict = { code: string; severity: "block"; message: string };
export type Command = {
  expectedRevision: number; // Personality revision, not configurationRevision.
  commandKey: string; // SHA-256 hex, derived by the authenticated dashboard BFF.
  actor: string; // Derived by BFF from verified identity, never the browser body.
};
export type Review = {
  persona?: string; // Full edited tone, 1..4096 chars; mandatory before approval.
  creatorFacts?: Partial<Record<FactKey, string | null>>; // Nonempty object if supplied.
  defaultModel?: string; // Existing native model catalogue identifier.
  replyModelOverride?: string | null; // null removes the ai_chatter override.
};
export type ImportRequest = Command;
export type ReviewRequest = Command & { review: Review }; // Complete replacement review.
export type ApprovalRequest = Command & {
  acknowledgements: { mixedAuthorship: true; runtimeRules: true; factsVerified: true };
};
export type Evidence = {
  id: string; sourceId: string; kind: "profile" | "outgoing_message";
  author: Author | "profile"; excerpt: string; truncated: boolean;
};
export type Observation = {
  key: string; value: string; evidenceIds: string[];
  attribution: Author | "profile"; selected: false;
};
export type Draft = {
  creatorFacts: (Observation & { key: FactKey; attribution: "profile" })[];
  writingStyle: Observation[]; // Key identity is [attribution,key].
  operationalInstructions: {
    text: string; evidenceIds: string[]; attribution: Author; selected: false;
    conflicts: Conflict[]; includedInConfiguration: false;
  }[];
  suggestedPersona: {
    text: string; attribution: Exclude<Author, "automation"> | null; evidenceIds: string[];
    selected: false; requiresReview: true; basis: "observed_account_style"; warning: string | null;
  };
  evidence: Evidence[];
  coverage: {
    inputMessages: number; uniqueMessages: number; sampledMessages: number;
    authorship: Record<Author, number>; method: string; limitations: string[];
    historyWindow?: string; ingressWindowLimit?: number; fullHistoryCoverage?: false;
    olderRowsNotScanned?: boolean; excludedOversizeOrBlank?: number;
    omittedForByteBudget?: number; profileSource?: string; sharedFactsState?: string;
    unverifiedPriorCreatorFactKeys?: FactKey[];
  };
};
export type Readiness = {
  ready: boolean; reviewRequired: true; blockers: string[];
  model: { state: string; modelId: string | null; provider: string | null;
    reasoningEffort: { state: string; saved: string | null; resolved: string | null } };
  credential: { state: string; configured: boolean; metadataAvailable: boolean };
  budget: { state: string; dailyCostCapCents: number | null };
  tone: { state: string; conflicts: Conflict[]; reviewRequired: true };
};
export type EmptyPersonality = {
  version: typeof PERSONALITY_VERSION; state: "empty"; revision: number; bindingId: string; document: null;
};
export type Personality = {
  version: typeof PERSONALITY_VERSION; state: "draft" | "approved"; revision: number; bindingId: string;
  document: { draft: Draft; review: Review; source: "canonical_history" | "authorized_export";
    approval?: { acknowledgements: ApprovalRequest["acknowledgements"];
      configurationCommandKey: string; appliedConfigurationRevision: number } };
  configurationRevision: number; configurationFingerprint: string; configurationCurrent: boolean;
  conflicts: Conflict[]; requiresFactResolution: FactKey[];
  factComparisons: { key: FactKey; current: string | null; proposed: string | null; changed: boolean }[];
  readiness: Readiness; createdAt: string; actor: string; commandKey: string;
};
export type History = {
  revisions: { revision: number; state: "draft" | "approved"; actor: string; createdAt: string }[];
  nextBefore: number | null;
};
export type Envelope<T = Personality | EmptyPersonality> = {
  schemaVersion: typeof CONFIGURATION_VERSION; ok: true; accountId: string;
  personalityVersion: typeof PERSONALITY_VERSION; personality: T; commandKey?: string;
};
export type Failure = {
  schemaVersion?: typeof CONFIGURATION_VERSION; ok: false; code: string; error: string;
};
/** Paths relative to /onlyfans/configuration/v1/accounts/{accountId}/personality:
 * GET ""; GET /history[?before=N]; GET /revisions/N;
 * POST /import ImportRequest; POST /review ReviewRequest; POST /approve ApprovalRequest.
 * Replays acknowledge their recorded revision; do not replace a newer current view
 * with a historical retry response. Refresh current state after every mutation.
 */
