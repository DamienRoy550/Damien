import type { Tool, KeyValueStore } from '../types';
import { ok, err } from '../types';

const PREFIX = 'note:';

interface Note {
  id: string;
  title: string;
  body: string;
  createdAt: string;
  updatedAt: string;
}

async function readAll(storage: KeyValueStore): Promise<Note[]> {
  const keys = await storage.keysWithPrefix(PREFIX);
  const notes: Note[] = [];
  for (const key of keys) {
    const raw = await storage.get(key);
    if (!raw) continue;
    try {
      notes.push(JSON.parse(raw) as Note);
    } catch {
      // skip corrupt entries
    }
  }
  notes.sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1));
  return notes;
}

function makeId(): string {
  return `n${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

/** Persistent notes the agent can write and query across sessions. */
export function createNoteTools(): Tool[] {
  const saveNote: Tool = {
    name: 'save_note',
    description:
      'Save a note so you (the assistant) can find it later, even after the app restarts. Use for anything the user asks to remember, store, or write down.',
    parameters: [
      { name: 'title', type: 'string', description: 'Short title, e.g. "Grocery list"', required: true },
      { name: 'body', type: 'string', description: 'The note content', required: true },
    ],
    runsOffline: true,
    async execute(args, ctx) {
      const title = String(args.title ?? '').trim();
      const body = String(args.body ?? '').trim();
      if (!title) return err('title is required');
      if (!body) return err('body is required');
      const nowIso = ctx.now().toISOString();
      const note: Note = { id: makeId(), title, body, createdAt: nowIso, updatedAt: nowIso };
      await ctx.storage.set(PREFIX + note.id, JSON.stringify(note));
      return ok(`Note saved. id: ${note.id}, title: "${title}". Confirm to the user and include the title.`);
    },
  };

  const listNotes: Tool = {
    name: 'list_notes',
    description: 'List saved notes (most recent first). Returns id, title and a short preview of each note.',
    parameters: [
      { name: 'limit', type: 'number', description: 'Max notes to list (default 10)' },
    ],
    runsOffline: true,
    async execute(args, ctx) {
      const notes = await readAll(ctx.storage);
      if (notes.length === 0) return ok('No notes saved yet.');
      const limit = Math.max(1, Math.min(50, Number(args.limit ?? 10) || 10));
      const lines = notes.slice(0, limit).map((n) => {
        const preview = n.body.length > 80 ? `${n.body.slice(0, 80)}…` : n.body;
        return `- [${n.id}] "${n.title}": ${preview}`;
      });
      if (notes.length > limit) lines.push(`(+ ${notes.length - limit} more)`);
      return ok(lines.join('\n'));
    },
  };

  const searchNotes: Tool = {
    name: 'search_notes',
    description: 'Search saved notes by keyword. Matching is case-insensitive across titles and bodies.',
    parameters: [
      { name: 'query', type: 'string', description: 'Keywords to search for', required: true },
    ],
    runsOffline: true,
    async execute(args, ctx) {
      const q = String(args.query ?? '').trim().toLowerCase();
      if (!q) return err('query is required');
      const notes = await readAll(ctx.storage);
      const words = q.split(/\s+/);
      const hits = notes.filter((n) => {
        const hay = `${n.title}\n${n.body}`.toLowerCase();
        return words.every((w) => hay.includes(w));
      });
      if (hits.length === 0) return ok(`No notes match "${q}".`);
      const lines = hits.slice(0, 10).map((n) => `- [${n.id}] "${n.title}": ${n.body}`);
      return ok(lines.join('\n'));
    },
  };

  const deleteNote: Tool = {
    name: 'delete_note',
    description: 'Delete a saved note by id (get ids from list_notes or search_notes).',
    parameters: [
      { name: 'id', type: 'string', description: 'The note id, e.g. "nabc123"', required: true },
    ],
    runsOffline: true,
    async execute(args, ctx) {
      const id = String(args.id ?? '').trim();
      if (!id) return err('id is required');
      const key = id.startsWith(PREFIX) ? id : PREFIX + id;
      const existing = await ctx.storage.get(key);
      if (!existing) return err(`No note with id "${id}". Use list_notes to find ids.`);
      await ctx.storage.delete(key);
      const parsed = JSON.parse(existing) as Note;
      return ok(`Deleted note "${parsed.title}" (${id}).`);
    },
  };

  return [saveNote, listNotes, searchNotes, deleteNote];
}
