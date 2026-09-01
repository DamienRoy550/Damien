import type { ChatMessage } from '../llm/types';
import type { StepRecord } from './types';
import { estimateMessagesTokens } from '../core/tokenizer';

export interface AgentTaskContextInput {
  systemPrompt: string;
  task: string;
  steps: StepRecord[];
  /** Prior conversation turns (user/assistant), oldest first. */
  history?: ChatMessage[];
}

/**
 * Assemble the message list for one model turn, and trim it to a token
 * budget. Small on-device models typically have 2k–8k context, so keeping
 * the prompt tight is critical:
 *
 *  1. System prompt and the user's task are NEVER trimmed.
 *  2. Oldest tool observations are replaced with "[omitted]" first —
 *     the model needs the latest observation most.
 *  3. Whole oldest steps are dropped next — which also eats conversation
 *     history first, since it sits directly after the task.
 *  4. The newest step is always kept verbatim (the model is mid-thought).
 */
export function buildMessages(
  input: AgentTaskContextInput,
  budgetTokens: number,
): ChatMessage[] {
  const messages: ChatMessage[] = [
    { role: 'system', content: input.systemPrompt },
    { role: 'user', content: input.task },
  ];

  if (input.history && input.history.length > 0) {
    messages.push(...input.history);
  }

  for (const step of input.steps) {
    messages.push({ role: 'assistant', content: serializeStepForModel(step) });
    messages.push({
      role: 'user',
      content: step.error
        ? `[OBSERVATION - ERROR] ${step.error}`
        : `[OBSERVATION] ${step.observation ?? '(no output)'}`,
    });
  }

  return trimToBudget(messages, budgetTokens);
}

/** The single place a recorded step is rendered back into model-speak. */
export function serializeStepForModel(step: StepRecord): string {
  return JSON.stringify({
    thought: step.thought,
    tool: step.tool,
    arguments: step.toolArguments ?? {},
  });
}

export function trimToBudget(messages: ChatMessage[], budgetTokens: number): ChatMessage[] {
  let working = messages.slice();
  if (estimateMessagesTokens(working) <= budgetTokens) return working;

  const headCount = 2; // system + task

  // Pass 1: keep only the newest observation verbatim.
  const lastObsIdx = findLastIndex(working, (m) => m.content.startsWith('[OBSERVATION'));
  working = working.map((m, i) =>
    m.content.startsWith('[OBSERVATION') && i !== lastObsIdx
      ? { ...m, content: '[OBSERVATION] [omitted to save space]' }
      : m,
  );

  if (estimateMessagesTokens(working) <= budgetTokens) return working;

  // Pass 2: drop oldest step pairs while over budget, keep head + last pair.
  while (working.length > headCount + 2 && estimateMessagesTokens(working) > budgetTokens) {
    working = [...working.slice(0, headCount), ...working.slice(headCount + 2)];
  }

  if (estimateMessagesTokens(working) > budgetTokens && working.length > headCount) {
    working = [...working.slice(0, headCount), ...working.slice(working.length - 2)];
  }

  return working;
}

function findLastIndex<T>(arr: T[], pred: (item: T) => boolean): number {
  for (let i = arr.length - 1; i >= 0; i--) {
    const item = arr[i];
    if (item !== undefined && pred(item)) return i;
  }
  return -1;
}
