/**
 * A dependency-free unified diff, for `save({dryRun: true})`: 3 lines of
 * context, `---`/`+++` headers naming the path, `\n` line endings.
 */

function splitLines(text: string): string[] {
  if (text === '') return [];
  const lines = text.split('\n');
  if (lines[lines.length - 1] === '') lines.pop();
  return lines;
}

type Op = { kind: ' ' | '-' | '+'; line: string };

/** Longest-common-subsequence diff. Config files are small; O(n·m) is fine. */
function diffOps(a: string[], b: string[]): Op[] {
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i]![j] = a[i] === b[j] ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
    }
  }
  const ops: Op[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ kind: ' ', line: a[i]! });
      i++;
      j++;
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
      ops.push({ kind: '-', line: a[i]! });
      i++;
    } else {
      ops.push({ kind: '+', line: b[j]! });
      j++;
    }
  }
  while (i < n) ops.push({ kind: '-', line: a[i++]! });
  while (j < m) ops.push({ kind: '+', line: b[j++]! });
  return ops;
}

/**
 * A unified diff of `before` against `after`. Returns `''` when the two texts
 * are identical. A missing target renders as a diff against an empty file.
 */
export function unifiedDiff(
  before: string,
  after: string,
  fromPath: string,
  toPath: string,
  context = 3,
): string {
  if (before === after) return '';
  const a = splitLines(before);
  const b = splitLines(after);
  const ops = diffOps(a, b);

  // Group changed regions, padded with `context` unchanged lines.
  const changed: number[] = [];
  ops.forEach((op, idx) => {
    if (op.kind !== ' ') changed.push(idx);
  });
  if (changed.length === 0) return '';

  const groups: [number, number][] = [];
  let start = changed[0]!;
  let end = changed[0]!;
  for (const idx of changed.slice(1)) {
    if (idx - end <= context * 2) {
      end = idx;
    } else {
      groups.push([start, end]);
      start = idx;
      end = idx;
    }
  }
  groups.push([start, end]);

  const out: string[] = [`--- ${fromPath}`, `+++ ${toPath}`];
  for (const [lo, hi] of groups) {
    const from = Math.max(0, lo - context);
    const to = Math.min(ops.length - 1, hi + context);
    let aStart = 0;
    let bStart = 0;
    for (let k = 0; k < from; k++) {
      const kind = ops[k]!.kind;
      if (kind !== '+') aStart++;
      if (kind !== '-') bStart++;
    }
    let aCount = 0;
    let bCount = 0;
    const body: string[] = [];
    for (let k = from; k <= to; k++) {
      const op = ops[k]!;
      if (op.kind !== '+') aCount++;
      if (op.kind !== '-') bCount++;
      body.push(op.kind + op.line);
    }
    out.push(
      `@@ -${aCount === 0 ? aStart : aStart + 1},${aCount} +${bCount === 0 ? bStart : bStart + 1},${bCount} @@`,
    );
    out.push(...body);
  }
  return out.join('\n') + '\n';
}
