import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { ConversationBoundary } from './ConversationBoundary';
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
test('contains a runtime failure and leaves recovery controls available', () => {
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  function Failure(): never { throw new Error('Connection interrupted'); }
  render(<div><nav>Organization navigation</nav><ConversationBoundary><Failure /></ConversationBoundary></div>);
  expect(screen.getByText('Organization navigation')).toBeTruthy();
  expect(screen.getByRole('alert')).toHaveProperty('textContent', 'Connection interrupted');
  expect(screen.getByRole('button', { name: 'Reconnect conversation' })).toBeTruthy();
});
