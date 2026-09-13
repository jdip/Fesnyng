import { afterEach, expect, test } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { ThreadContext } from './ThreadContext';

afterEach(cleanup);

test('retired toolbar has no controls', () => {
  render(<ThreadContext organization="org" agent="junior" session="thread" csrf="csrf-example" />);
  expect(screen.queryByRole('button', { name: 'Thread activity' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Thread permissions' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Files' })).toBeNull();
});
