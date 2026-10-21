// Flat config, typescript-eslint, type-aware. No Prettier: `.editorconfig`
// governs whitespace and the stylistic rules here are the ones that catch
// bugs, not the ones that argue about commas.
import js from '@eslint/js';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: ['dist/**', 'coverage/**', 'docs-api/**', 'node_modules/**'],
  },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // The library talks to an API whose bodies are `unknown` until decoded;
      // the decoders narrow explicitly and are the only place this is allowed.
      '@typescript-eslint/restrict-template-expressions': [
        'error',
        { allowNumber: true, allowBoolean: true, allowNullish: true },
      ],
      '@typescript-eslint/no-non-null-assertion': 'off',
      'no-console': ['error', { allow: ['warn'] }],
    },
  },
  {
    files: ['test/**/*.ts', 'examples/**/*.ts', 'scripts/**/*.mjs'],
    rules: {
      // node:test's `test()` returns a promise that the runner owns.
      '@typescript-eslint/no-floating-promises': 'off',
      '@typescript-eslint/no-unnecessary-type-assertion': 'off',
      '@typescript-eslint/no-unnecessary-condition': 'off',
      '@typescript-eslint/no-unsafe-assignment': 'off',
      '@typescript-eslint/no-unsafe-member-access': 'off',
      '@typescript-eslint/no-unsafe-call': 'off',
      '@typescript-eslint/no-non-null-assertion': 'off',
      'no-console': 'off',
    },
  },
  {
    // The cross-check harness is a JSON bridge: every value crossing stdin is
    // `any` by construction, and the point of the file is to report what the
    // library did with it, not to re-type the protocol.
    files: ['cross_check/**/*.ts'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-unsafe-assignment': 'off',
      '@typescript-eslint/no-unsafe-member-access': 'off',
      '@typescript-eslint/no-unsafe-call': 'off',
      '@typescript-eslint/no-unsafe-argument': 'off',
      '@typescript-eslint/no-unsafe-return': 'off',
      '@typescript-eslint/no-non-null-assertion': 'off',
      '@typescript-eslint/no-unnecessary-condition': 'off',
      '@typescript-eslint/restrict-template-expressions': 'off',
      'no-console': 'off',
    },
  },
  {
    // Plain JS tooling files are not part of the TS program.
    files: ['**/*.js', '**/*.mjs'],
    extends: [tseslint.configs.disableTypeChecked],
  },
  {
    files: ['src/cli.ts'],
    rules: {
      // The CLI is the one place that writes to stdout/stderr by design.
      'no-console': 'off',
    },
  },
);
