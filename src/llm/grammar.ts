/**
 * GBNF grammar that constrains generation to exactly one JSON object.
 * Used by llama.cpp-based engines when "Strict JSON mode" is enabled and
 * the engine has no native response_format support.
 *
 * Format documented to the model (see prompts.ts):
 *   {"thought": "...", "tool": "...", "arguments": {...}}
 *   {"thought": "...", "answer": "..."}
 *
 * Kept generic-JSON (not schema-exact) so models can also produce plain
 * objects if they drift slightly — the wire parser handles the rest.
 */
export const JSON_GRAMMAR = String.raw`root ::= object

object ::= "{" ws ( string ":" ws value ("," ws string ":" ws value)* )? "}" ws

value ::= object | array | string | number | ("true" | "false" | "null") ws

array ::= "[" ws ( value ("," ws value)* )? "]" ws

string ::= "\"" ( [^"\\\x7F\x00-\x1F] | "\\" (["\\bfnrt] | "u" [0-9a-fA-F]{4}) )* "\"" ws

number ::= ("-"? ([0-9] | [1-9] [0-9]{0,15})) ("." [0-9]+)? ([eE] [-+]? [0-9] [1-9]{0,15})? ws

ws ::= | " " | "\n" [ \t]{0,20}`;
