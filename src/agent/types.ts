import type { ToolName, JsonRecord } from '../tools/types';

/** What the model actually said for one step, parsed into a machine shape. */
export type ModelReply =
  | {
      kind: 'tool';
      thought?: string;
      tool: ToolName;
      arguments: JsonRecord;
      raw: string;
    }
  | {
      kind: 'answer';
      thought?: string;
      answer: string;
      raw: string;
    };

export interface StepRecord {
  index: number; // 1-based
  thought?: string;
  tool?: ToolName;
  toolArguments?: JsonRecord;
  observation?: string;
  observationTruncated?: boolean;
  error?: string;
  durationMs?: number;
}

export type RunStatus = 'ok' | 'error' | 'cancelled' | 'max_steps';

export interface RunResult {
  status: RunStatus;
  answer: string;
  steps: StepRecord[];
  tokensEstimated: number;
  elapsedMs: number;
}

export type AgentEvent =
  | { type: 'run_started'; task: string; maxSteps: number }
  | { type: 'reply_started'; step: number }
  | { type: 'token'; step: number; delta: string; full: string }
  | { type: 'model_reply'; step: number; reply: ModelReply }
  | { type: 'tool_started'; step: number; tool: ToolName; args: JsonRecord }
  | { type: 'tool_finished'; step: number; tool: ToolName; observation: string }
  | { type: 'tool_error'; step: number; tool: ToolName; error: string }
  | { type: 'finished'; result: RunResult }
  | { type: 'failed'; error: string }
  | { type: 'cancelled' };

export type AgentEventHandler = (event: AgentEvent) => void;

export interface AgentRunOptions {
  maxSteps?: number;
  temperature?: number;
  maxAnswerTokens?: number;
  maxToolTokens?: number;
  /** Hard cap on characters kept from any single tool observation. */
  observationCharLimit?: number;
  signal?: AbortSignal;
  onEvent?: AgentEventHandler;
}
