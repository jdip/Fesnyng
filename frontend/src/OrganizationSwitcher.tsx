import { DropdownMenu } from 'radix-ui';
import { CheckIcon, ChevronsUpDownIcon, PlusIcon } from 'lucide-react';
import { OrganizationIcon } from './OrganizationIcon';
import type { Organization } from './workspace-api';

export function OrganizationSwitcher({ organizations, selected, onSelect }: { organizations: Organization[]; selected: string; onSelect: (id: string) => void }) {
  const current = organizations.find((organization) => organization.id === selected);
  if (!current) return null;
  return <div className="organization-switcher"><OrganizationIcon organization={current} /><strong className="organization-name">{current.name}</strong>
    <DropdownMenu.Root><DropdownMenu.Trigger asChild><button className="organization-switch-trigger" aria-label={`Switch organization: ${current.name}`} title="Switch organization"><ChevronsUpDownIcon size={16} aria-hidden="true" /></button></DropdownMenu.Trigger>
      <DropdownMenu.Portal><DropdownMenu.Content className="organization-menu" sideOffset={8} align="start" aria-label="Organizations">
        <DropdownMenu.RadioGroup value={selected} onValueChange={onSelect}>{organizations.map((organization) => <DropdownMenu.RadioItem className="organization-menu-item" key={organization.id} value={organization.id}>
          <OrganizationIcon organization={organization} /><span>{organization.name}</span><DropdownMenu.ItemIndicator><CheckIcon size={16} aria-hidden="true" /></DropdownMenu.ItemIndicator>
        </DropdownMenu.RadioItem>)}</DropdownMenu.RadioGroup>
        <DropdownMenu.Separator className="organization-menu-separator" />
        <DropdownMenu.Item className="organization-menu-item" onSelect={() => onSelect('__new__')}><PlusIcon size={16} aria-hidden="true" /><span>Create organization</span></DropdownMenu.Item>
      </DropdownMenu.Content></DropdownMenu.Portal>
    </DropdownMenu.Root>
  </div>;
}
