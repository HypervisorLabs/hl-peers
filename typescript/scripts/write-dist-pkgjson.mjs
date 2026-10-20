// Mark each build output with its module system, so Node resolves
// dist/cjs/*.js as CommonJS under a "type": "module" package root.
import { mkdirSync, writeFileSync } from 'node:fs';

for (const [dir, type] of [
  ['dist/esm', 'module'],
  ['dist/cjs', 'commonjs'],
]) {
  mkdirSync(dir, { recursive: true });
  writeFileSync(`${dir}/package.json`, JSON.stringify({ type }, null, 2) + '\n');
}
