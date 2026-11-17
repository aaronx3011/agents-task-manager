/** Preserve the full reply while keeping each synthesis request short. */
export function speechChunks(text: string, limit = 900): string[] {
  if (!Number.isInteger(limit) || limit < 2 || limit > 2000)
    throw new Error("Invalid speech chunk limit");
  const chunks: string[] = [];
  while (text.length > limit) {
    const prefix = text.slice(0, limit);
    let end = 0;
    for (const match of prefix.matchAll(/[.!?。！？]["'”’)]*\s+|\n\s*\n/gu))
      end = match.index + match[0].length;
    if (end < limit / 3) {
      end = prefix.lastIndexOf(" ") + 1;
      if (end < limit / 3) end = limit;
    }
    // Never divide a surrogate pair in an unbroken URL, word or emoji sequence.
    if (/[\uD800-\uDBFF]/u.test(text[end - 1])) end--;
    chunks.push(text.slice(0, end));
    text = text.slice(end);
  }
  if (text) chunks.push(text);
  return chunks;
}

/** At most one chunk is prefetched while the current chunk plays. */
export async function playSpeech(
  text: string,
  synthesize: (text: string, signal: AbortSignal) => Promise<Blob>,
  play: (blob: Blob, signal: AbortSignal) => Promise<void>,
  signal: AbortSignal,
) {
  const chunks = speechChunks(text).filter((chunk) => chunk.trim());
  const load = (index: number) =>
    synthesize(chunks[index], signal).then(
      (blob) => ({ blob, error: undefined }),
      (error: unknown) => ({ blob: undefined, error }),
    );
  if (!chunks.length) return;
  signal.throwIfAborted();
  let next = load(0);
  for (let index = 0; index < chunks.length; index++) {
    const result = await next;
    signal.throwIfAborted();
    if (!result.blob) throw result.error;
    if (index + 1 < chunks.length) next = load(index + 1);
    await play(result.blob, signal);
    signal.throwIfAborted();
  }
}
