import type { FetchLike } from './net';

/**
 * Native platforms have no CORS restrictions — straight fetch, full
 * internet access (that is how web_fetch / http_request / web_search work
 * on the phone).
 */
export const netFetch: FetchLike = fetch.bind(globalThis);
