import js from '@eslint/js';

export default [js.configs.recommended, {
  languageOptions: {
    globals: Object.fromEntries(['process', 'fetch', 'Request', 'Response', 'Headers', 'AbortSignal', 'URL', 'setTimeout'].map(name => [name, 'readonly'])),
  },
}];
