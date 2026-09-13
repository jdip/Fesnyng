import { afterEach, expect, test } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { ThreadContext } from './ThreadContext';

afterEach(cleanup);

test('has only the remaining permissions control', () => {
  render(<ThreadContext organization="org" agent="junior" session="thread" csrf="csrf-example" />);
  expect(screen.queryByRole('button', { name: 'Thread activity' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Thread permissions' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Files' })).toBeNull();
});
