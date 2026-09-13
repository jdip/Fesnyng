import { afterEach, expect, test } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { NativeEditToolFallback } from './native-edit-tool';

afterEach(cleanup);

test('renders a collapsed structured diff for a native OpenCode edit result', () => {
  const props = {
    toolName: 'edit',
    argsText: JSON.stringify({
      filePath: 'src/agent.ts', oldString: 'const mode = "old";', newString: 'const mode = "new";',
    }),
    result: { output: 'updated' },
    status: { type: 'complete' },
  } as unknown as ComponentProps<typeof NativeEditToolFallback>;

  render(
    <NativeEditToolFallback {...props} />,
  );

  expect(screen.getAllByText('src/agent.ts')).toHaveLength(2);
  expect(screen.getByLabelText('Structured file changes').textContent).toContain('const mode = "new";');
});

test('renders native apply_patch file changes instead of a generic patch payload', () => {
  const props = {
    toolName: 'apply_patch',
    argsText: JSON.stringify({
      patchText: [
        '*** Begin Patch',
        '*** Update File: src/agent.ts',
        '-const mode = "old";',
        '+const mode = "new";',
        '*** Add File: src/new.ts',
        '+export const ready = true;',
        '*** End Patch',
      ].join('\n'),
    }),
    result: { output: 'Done!' },
    status: { type: 'complete' },
  } as unknown as ComponentProps<typeof NativeEditToolFallback>;

  render(<NativeEditToolFallback {...props} />);

  expect(screen.getByText('Applied patch to 2 files')).toBeTruthy();
  expect(screen.getByLabelText('changed src/agent.ts').textContent).toContain('const mode = "new";');
  expect(screen.getByLabelText('added src/new.ts').textContent).toContain('export const ready = true;');
});

test('keeps a failed native edit on the maintained fallback with its error', () => {
  const props = {
    toolName: 'apply_patch',
    argsText: JSON.stringify({
      patchText: '*** Begin Patch\n*** Update File: src/agent.ts\n-old\n+new\n*** End Patch',
    }),
    result: 'Patch did not apply',
    isError: true,
    status: { type: 'complete' },
  } as unknown as ComponentProps<typeof NativeEditToolFallback>;

  render(<NativeEditToolFallback {...props} />);

  expect(screen.queryByText('Edited')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /Used tool: apply_patch/ }));
  expect(screen.getByText('Patch did not apply')).toBeTruthy();
});
