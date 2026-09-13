import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { OrganizationSwitcher } from './OrganizationSwitcher';

afterEach(cleanup);
const organizations = [{ id: 'one', name: 'First organization' }, { id: 'two', name: 'Second organization', icon: { kind: 'emoji' as const, value: '🌿' } }];

test('opens an accessible organization menu with keyboard selection and creation', async () => {
  const onSelect = vi.fn();
  render(<OrganizationSwitcher organizations={organizations} selected="one" onSelect={onSelect} />);
  const trigger = screen.getByRole('button', { name: 'Switch organization: First organization' });
  trigger.focus();
  fireEvent.keyDown(trigger, { key: 'ArrowDown' });
  const next = await screen.findByRole('menuitemradio', { name: 'Second organization' });
  expect(screen.getByRole('menuitemradio', { name: 'First organization' }).getAttribute('aria-checked')).toBe('true');
  fireEvent.click(next);
  expect(onSelect).toHaveBeenCalledWith('two');
  await waitFor(() => expect(document.activeElement).toBe(trigger));
  fireEvent.keyDown(trigger, { key: 'ArrowDown' });
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Create organization' }));
  expect(onSelect).toHaveBeenCalledWith('__new__');
});

test('shows initials fallback and configured identity without app branding', () => {
  const { rerender } = render(<OrganizationSwitcher organizations={organizations} selected="one" onSelect={() => {}} />);
  expect(screen.getByText('FO')).toBeTruthy();
  expect(screen.queryByText('Fesnyng')).toBeNull();
  rerender(<OrganizationSwitcher organizations={organizations} selected="two" onSelect={() => {}} />);
  expect(screen.getByText('🌿')).toBeTruthy();
  expect(screen.queryByText('FO')).toBeNull();
});

test('keeps chart and role-authorized settings actions with their organization entry', async () => {
  const onSelect = vi.fn();
  const onOpen = vi.fn();
  render(<OrganizationSwitcher organizations={organizations} selected="one" managerOrganizationIds={['two']} onSelect={onSelect} onOpen={onOpen} />);
  const trigger = screen.getByRole('button', { name: 'Switch organization: First organization' });
  fireEvent.keyDown(trigger, { key: 'ArrowDown' });

  fireEvent.click(await screen.findByRole('menuitem', { name: 'Reporting chart for First organization' }));
  expect(onOpen).toHaveBeenCalledWith('one', 'chart');
  fireEvent.keyDown(trigger, { key: 'ArrowDown' });
  expect(screen.queryByRole('menuitem', { name: 'Organization settings for First organization' })).toBeNull();
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Organization settings for Second organization' }));
  expect(onOpen).toHaveBeenCalledWith('two', 'organization');
});
