import { expect, test } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { WrenchIcon } from 'lucide-react';
import { Collapsible, CollapsibleContent } from '@/components/ui/collapsible';
import { GroupPreviewTrigger } from './group-preview';
import { ToolFallback } from './tool-fallback.aui';

test('uses one compact row and expands when its far-right chevron is clicked', () => {
  const { container } = render(
    <Collapsible>
      <GroupPreviewTrigger icon={WrenchIcon} label="1 tool call" preview="Read the report" />
      <CollapsibleContent>Tool details</CollapsibleContent>
    </Collapsible>,
  );

  const trigger = screen.getByRole('button', { name: '1 tool call: Read the report' });
  const chevron = container.querySelector('[data-slot="group-preview-trigger-chevron"]');
  expect(trigger.className.split(' ')).toContain('py-1');
  expect(chevron).toBeTruthy();
  expect(chevron!.getAttribute('class')?.split(' ')).toContain('group-data-[state=open]/trigger:rotate-0');
  fireEvent.click(chevron!);
  expect(trigger.getAttribute('aria-expanded')).toBe('true');
  expect(screen.getByText('Tool details')).toBeTruthy();
});

test('rotates a standalone tool chevron with its collapsible state', () => {
  const { container } = render(
    <ToolFallback.Root>
      <ToolFallback.Trigger toolName="read" status={{ type: 'complete' }} />
      <ToolFallback.Content>Tool details</ToolFallback.Content>
    </ToolFallback.Root>,
  );

  const trigger = screen.getByRole('button', { name: /Used tool: read/ });
  const chevron = container.querySelector('[data-slot="tool-fallback-trigger-chevron"]');
  expect(chevron).toBeTruthy();
  expect(trigger.getAttribute('aria-expanded')).toBe('false');
  expect(chevron!.getAttribute('class')?.split(' ')).toContain('group-data-[state=open]/trigger:rotate-0');
  fireEvent.click(chevron!);
  expect(trigger.getAttribute('data-state')).toBe('open');
});
