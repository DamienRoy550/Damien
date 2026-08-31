import type { Tool, ToolParam } from './types';

/**
 * Central tool registry. The agent may only call tools registered here —
 * the registry is also what generates the system-prompt tool documentation.
 */
export class ToolRegistry {
  private readonly map = new Map<string, Tool>();

  register(tool: Tool): void {
    if (this.map.has(tool.name)) {
      throw new Error(`Tool "${tool.name}" is already registered`);
    }
    this.map.set(tool.name, tool);
  }

  get(name: string): Tool | undefined {
    return this.map.get(name);
  }

  has(name: string): boolean {
    return this.map.has(name);
  }

  get all(): Tool[] {
    return Array.from(this.map.values());
  }

  get names(): string[] {
    return Array.from(this.map.keys());
  }
}

export type { Tool, ToolParam };
