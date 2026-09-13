import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ThreadPagePreferenceForm, useThreadPagePreference } from './ThreadPagePreference';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function PreferenceHarness({ show = true, onChange }: { show?: boolean; onChange: (size: number) => void }) {
  const preference = useThreadPagePreference({ organization: 'org', csrf: 'csrf-example', onChange });
  return show ? <ThreadPagePreferenceForm preference={preference} /> : null;
}

test('loads and saves the personal page size without applying a failed write', async () => {
  let fail = false;
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => new Response(JSON.stringify(fail ? { detail: 'Save unavailable' } : { thread_list_page_size: init.method === 'PUT' ? 12 : 6 }), { status: fail ? 503 : 200 })));
  const changed = vi.fn();
  render(<PreferenceHarness onChange={changed} />);
  await waitFor(() => expect(changed).toHaveBeenCalledWith(6));
  fireEvent.change(screen.getByLabelText('Threads per page'), { target: { value: '12' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save page size' }));
  await waitFor(() => expect(changed).toHaveBeenCalledWith(12));
  fail = true;
  fireEvent.change(screen.getByLabelText('Threads per page'), { target: { value: '24' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save page size' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Save unavailable');
  expect(changed).not.toHaveBeenCalledWith(24);
});

test('loads the personal page size while its panel remains closed', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ thread_list_page_size: 12 }))));
  const changed = vi.fn();
  const rendered = render(<PreferenceHarness show={false} onChange={changed} />);
  await waitFor(() => expect(changed).toHaveBeenCalledWith(12));
  expect(screen.queryByLabelText('Threads per page')).toBeNull();
  rendered.rerender(<PreferenceHarness onChange={changed} />);
  expect(screen.getByLabelText('Threads per page')).toHaveProperty('value', '12');
});
