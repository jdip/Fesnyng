import { expect, test } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { WrenchIcon } from 'lucide-react';
import { Collapsible, CollapsibleContent } from '@/components/ui/collapsible';
import { GroupPreviewTrigger } from './group-preview';

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
  fireEvent.click(chevron!);
  expect(trigger.getAttribute('aria-expanded')).toBe('true');
  expect(screen.getByText('Tool details')).toBeTruthy();
});
