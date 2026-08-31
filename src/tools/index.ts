import { ToolRegistry } from './registry';
import type { ToolContext } from './types';
import { calculator } from './builtin/calculator';
import { unitConvert } from './builtin/unitConvert';
import { dateTime } from './builtin/dateTime';
import { createNoteTools } from './builtin/notes';
import { webFetch } from './builtin/webFetch';
import { httpRequest } from './builtin/httpRequest';
import { createReminderTool } from './builtin/reminder';
import { textStats } from './builtin/textStats';
import { clipboardTool } from './builtin/device';
import { openWebsite, openApp } from './builtin/apps';

/**
 * The default Damien toolbox. Order matters — it's the order tools are
 * documented to the model, so most-used first.
 */
export function createDefaultRegistry(ctx: ToolContext): ToolRegistry {
  const registry = new ToolRegistry();
  registry.register(calculator);
  registry.register(unitConvert);
  registry.register(dateTime);
  for (const t of createNoteTools()) registry.register(t);
  registry.register(webFetch);
  registry.register(httpRequest);
  registry.register(textStats);
  if (ctx.device) {
    registry.register(clipboardTool);
    registry.register(openWebsite);
    registry.register(openApp);
  }
  if (ctx.scheduler) {
    registry.register(createReminderTool());
  }
  return registry;
}
