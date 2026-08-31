import type { LLMEngine } from '../llm/types';
import { ToolRegistry } from '../tools/registry';
import type { ToolContext } from '../tools/types';
import { coerceArguments } from '../tools/types';
import { buildSystemPrompt } from './prompts';
import { parseModelReply } from './parser';
import { buildMessages } from './context';
import type { AgentEvent, AgentRunOptions, RunResult, StepRecord, RunStatus } from './types';
import { estimateTokens } from '../core/tokenizer';

const DEFAULT_MAX_STEPS = 6;
const DEFAULT_OBSERVATION_CHARS = 900;
const TOOL_TIMEOUT_MS = 25_000;

export interface AgentEngineConfig {
  /** Fraction of the model context usable for the conversation prompt. */
  contextBudgetFraction?: number;
  timeZone?: string;
  /** Extra standing instructions appended to the system prompt. */
  extraInstructions?: string;
}

export class AgentEngine {
  constructor(
    private readonly engine: LLMEngine,
    private readonly registry: ToolRegistry,
    private readonly toolContext: ToolContext,
    private readonly config: AgentEngineConfig = {},
  ) {}

  /**
   * Run one task through the full plan→act→observe loop.
   * Emits granular events so any UI can render live progress.
   */
  async run(task: string, options: AgentRunOptions = {}): Promise<RunResult> {
    const startedAt = Date.now();
    const maxSteps = options.maxSteps ?? DEFAULT_MAX_STEPS;
    const obsLimit = options.observationCharLimit ?? DEFAULT_OBSERVATION_CHARS;
    const emit = (e: AgentEvent) => options.onEvent?.(e);

    const systemPrompt = buildSystemPrompt(
      this.registry.all,
      this.toolContext.now(),
      this.config.timeZone,
      this.config.extraInstructions,
    );

    const budget = Math.floor(
      (this.engine.info?.contextSize ?? 3072) * (this.config.contextBudgetFraction ?? 0.6),
    );

    const steps: StepRecord[] = [];
    let tokensEstimated = estimateTokens(systemPrompt);
    let cancelled = false;

    emit({ type: 'run_started', task, maxSteps });

    for (let stepIndex = 1; stepIndex <= maxSteps; stepIndex++) {
      if (options.signal?.aborted) {
        cancelled = true;
        break;
      }

      const messages = buildMessages(
        { systemPrompt, task, steps, history: options.history },
        budget,
      );
      emit({ type: 'reply_started', step: stepIndex });

      let replyText = '';
      try {
        const completion = await this.engine.complete({
          messages,
          maxTokens: options.maxAnswerTokens ?? 500,
          temperature: options.temperature ?? 0.2,
          stopSequences: ['\n{"thought"', '[OBSERVATION'],
          forceJson: true,
          signal: options.signal,
          onToken: (delta, full) => emit({ type: 'token', step: stepIndex, delta, full }),
        });
        replyText = completion.text;
        tokensEstimated += estimateTokens(messages.map((m) => m.content).join('\n')) + estimateTokens(replyText);
      } catch (e) {
        if (options.signal?.aborted) {
          cancelled = true;
          break;
        }
        const failed: RunResult = {
          status: 'error',
          answer: `The model failed to generate: ${e instanceof Error ? e.message : String(e)}`,
          steps,
          tokensEstimated,
          elapsedMs: Date.now() - startedAt,
        };
        emit({ type: 'failed', error: failed.answer });
        return failed;
      }

      if (options.signal?.aborted) {
        cancelled = true;
        break;
      }

      const reply = parseModelReply(replyText);
      emit({ type: 'model_reply', step: stepIndex, reply });

      if (reply.kind === 'answer') {
        const result: RunResult = {
          status: 'ok',
          answer: reply.answer,
          steps,
          tokensEstimated,
          elapsedMs: Date.now() - startedAt,
        };
        emit({ type: 'finished', result });
        return result;
      }

      // ---- Tool step ----
      const record: StepRecord = {
        index: stepIndex,
        thought: reply.thought,
        tool: reply.tool,
        toolArguments: reply.arguments,
      };
      steps.push(record);

      emit({ type: 'tool_started', step: stepIndex, tool: reply.tool, args: reply.arguments });

      const tool = this.registry.get(reply.tool);
      if (!tool) {
        record.error = `Unknown tool "${reply.tool}"`;
        const observation = `ERROR: unknown tool "${reply.tool}". Available tools: ${this.registry.names.join(', ')}. Repeat your reply as one JSON object using a valid tool, or give your final "answer".`;
        record.observation = observation;
        emit({ type: 'tool_error', step: stepIndex, tool: reply.tool, error: record.error });
        continue;
      }

      const coerced = coerceArguments(tool, reply.arguments);
      if (!coerced.ok) {
        record.error = coerced.error;
        record.observation = `ERROR: ${coerced.error}`;
        emit({ type: 'tool_error', step: stepIndex, tool: reply.tool, error: coerced.error });
        continue;
      }
      record.toolArguments = coerced.args;

      const toolStarted = Date.now();
      try {
        const result = await withTimeout(
          tool.execute(coerced.args, this.toolContext),
          TOOL_TIMEOUT_MS,
          `Tool "${tool.name}" timed out after ${TOOL_TIMEOUT_MS / 1000}s`,
        );
        record.durationMs = Date.now() - toolStarted;
        const observation =
          result.output.length > obsLimit
            ? `${result.output.slice(0, obsLimit)}… [truncated]`
            : result.output;
        record.observation = observation;
        record.observationTruncated = result.output.length > obsLimit;

        if (!result.ok) {
          record.error = result.error ?? 'tool error';
          emit({ type: 'tool_error', step: stepIndex, tool: reply.tool, error: record.error });
        } else {
          emit({ type: 'tool_finished', step: stepIndex, tool: reply.tool, observation });
        }
      } catch (e) {
        record.durationMs = Date.now() - toolStarted;
        const msg = e instanceof Error ? e.message : String(e);
        record.error = msg;
        record.observation = `ERROR: ${msg}`;
        emit({ type: 'tool_error', step: stepIndex, tool: reply.tool, error: msg });
        if (options.signal?.aborted) {
          cancelled = true;
          break;
        }
      }
    }

    if (cancelled) {
      const result: RunResult = {
        status: 'cancelled',
        answer: '',
        steps,
        tokensEstimated,
        elapsedMs: Date.now() - startedAt,
      };
      emit({ type: 'cancelled' });
      return result;
    }

    // ---- Out of steps: one final forced-answer turn ----
    if (options.signal?.aborted) {
      const result: RunResult = {
        status: 'cancelled',
        answer: '',
        steps,
        tokensEstimated,
        elapsedMs: Date.now() - startedAt,
      };
      emit({ type: 'cancelled' });
      return result;
    }

    emit({ type: 'reply_started', step: maxSteps + 1 });
    try {
      const forced = buildMessages(
        {
          systemPrompt,
          task,
          history: options.history,
          steps: [
            ...steps,
            {
              index: steps.length + 1,
              thought: 'Budget exhausted — must answer now.',
              observation: '[SYSTEM] You have used all your tool steps. Respond NOW with {"thought":"...","answer":"..."} using what you have.',
            },
          ],
        },
        budget,
      );
      const completion = await this.engine.complete({
        messages: forced,
        maxTokens: options.maxAnswerTokens ?? 500,
        temperature: options.temperature ?? 0.2,
        forceJson: true,
        signal: options.signal,
      });
      tokensEstimated += estimateTokens(completion.text);
      const reply = parseModelReply(completion.text);
      const status: RunStatus = 'max_steps';
      const result: RunResult = {
        status,
        answer: reply.kind === 'answer' ? reply.answer : summarizeSteps(steps),
        steps,
        tokensEstimated,
        elapsedMs: Date.now() - startedAt,
      };
      emit({ type: 'finished', result });
      return result;
    } catch {
      const result: RunResult = {
        status: 'max_steps',
        answer: summarizeSteps(steps),
        steps,
        tokensEstimated,
        elapsedMs: Date.now() - startedAt,
      };
      emit({ type: 'finished', result });
      return result;
    }
  }
}

function summarizeSteps(steps: StepRecord[]): string {
  if (steps.length === 0) return 'I was unable to complete this task.';
  const last = steps[steps.length - 1] as StepRecord;
  return `I used my ${steps.length} available steps. Last result: ${last.observation ?? 'none'}.`;
}

async function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
