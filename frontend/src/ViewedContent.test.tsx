import { afterEach, expect, test, vi } from 'vitest';
import { act, cleanup, render } from '@testing-library/react';
import { ViewedContent } from './ViewedContent';

let observe: (visible: boolean) => void;
class Observer {
  constructor(callback: IntersectionObserverCallback) { observe = (visible) => callback([{ isIntersecting: visible } as IntersectionObserverEntry], this as unknown as IntersectionObserver); }
  observe() {}
  disconnect() {}
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

test('acknowledges visible content only after its tail enters the visible document', () => {
  vi.stubGlobal('IntersectionObserver', Observer);
  let visibility: DocumentVisibilityState = 'hidden';
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
  const viewed = vi.fn();
  render(<ViewedContent identity="native:result" onView={viewed} />);
  expect(viewed).not.toHaveBeenCalled();
  act(() => observe(true));
  expect(viewed).not.toHaveBeenCalled();
  visibility = 'visible';
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  expect(viewed).toHaveBeenCalledTimes(1);
  act(() => observe(true));
  expect(viewed).toHaveBeenCalledTimes(1);
});

test('does not acknowledge offscreen content and observes a newly rendered result separately', () => {
  vi.stubGlobal('IntersectionObserver', Observer);
  const viewed = vi.fn();
  const { rerender } = render(<ViewedContent identity="native:first" onView={viewed} />);
  act(() => observe(false));
  expect(viewed).not.toHaveBeenCalled();
  act(() => observe(true));
  expect(viewed).toHaveBeenCalledTimes(1);
  rerender(<ViewedContent identity="native:second" onView={viewed} />);
  expect(viewed).toHaveBeenCalledTimes(1);
  act(() => observe(true));
  expect(viewed).toHaveBeenCalledTimes(2);
});

test('retries a failed read acknowledgement when the user returns to the page', async () => {
  vi.stubGlobal('IntersectionObserver', Observer);
  const viewed = vi.fn().mockResolvedValueOnce(false).mockResolvedValue(true);
  render(<ViewedContent identity="native:result" onView={viewed} />);
  await act(async () => { observe(true); });
  expect(viewed).toHaveBeenCalledTimes(1);
  await act(async () => { window.dispatchEvent(new Event('focus')); });
  expect(viewed).toHaveBeenCalledTimes(2);
});
