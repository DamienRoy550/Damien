/**
 * Token estimation without a tokenizer.
 *
 * The agent core must run in plain Node (tests, CLI) where no llama.cpp
 * tokenizer exists, so we use a calibrated heuristic: English + JSON mixes
 * average roughly 3.5–4 characters per token. We bias slightly high (3.6)
 * so the context trimmer errs on the safe side.
 */
export function estimateTokens(text: string): number {
  if (!text) return 0;
  // JSON/code is denser per character; prose is sparser. Split the difference.
  return Math.ceil(text.length / 3.6);
}

export function estimateMessagesTokens(messages: { content: string }[]): number {
  // +4 tokens per message for role framing overhead.
  return messages.reduce((sum, m) => sum + estimateTokens(m.content) + 4, 0);
}
