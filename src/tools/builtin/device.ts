import type { Tool } from '../types';
import { ok, err } from '../types';

/** Copy text to the phone's clipboard (only registered when ctx.device exists). */
export const clipboardTool: Tool = {
  name: 'copy_to_clipboard',
  description: 'Copy text to the phone clipboard so the user can paste it anywhere.',
  parameters: [
    { name: 'text', type: 'string', description: 'Text to copy', required: true },
  ],
  runsOffline: true,
  async execute(args, ctx) {
    const text = String(args.text ?? '');
    if (!text) return err('text is empty');
    if (!ctx.device) return err('Clipboard is not available.');
    await ctx.device.copyToClipboard(text);
    return ok(`Copied ${text.length} characters to the clipboard. Confirm briefly to the user.`);
  },
};
