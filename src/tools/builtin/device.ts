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

/** Open a URL / deep link in the phone browser or matching app. */
export const openLinkTool: Tool = {
  name: 'open_link',
  description:
    'Open a URL or app deep link on the phone (browser, maps, mailto:, tel:, spotify:, etc). Use when the user asks to open/show/launch something.',
  parameters: [
    { name: 'url', type: 'string', description: 'The URL or deep link to open', required: true },
  ],
  runsOffline: true,
  async execute(args, ctx) {
    const url = String(args.url ?? '').trim();
    if (!url) return err('url is required');
    if (!ctx.device) return err('open_link is not available.');
    await ctx.device.openUrl(url);
    return ok(`Opened ${url}. Confirm briefly to the user.`);
  },
};
