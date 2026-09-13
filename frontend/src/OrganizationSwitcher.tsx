import { DropdownMenu } from 'radix-ui';
import { CheckIcon, ChevronsUpDownIcon, NetworkIcon, PlusIcon, SettingsIcon } from 'lucide-react';
import { OrganizationIcon } from './OrganizationIcon';
import type { Organization } from './workspace-api';

type OrganizationView = 'chart' | 'organization';

export function OrganizationSwitcher({ organizations, selected, managerOrganizationIds = [], onSelect, onOpen }: { organizations: Organization[]; selected: string; managerOrganizationIds?: string[]; onSelect: (id: string) => void; onOpen?: (id: string, view: OrganizationView) => void }) {
  const current = organizations.find((organization) => organization.id === selected);
  if (!current) return null;
  return <div className="organization-switcher"><OrganizationIcon organization={current} /><strong className="organization-name">{current.name}</strong>
    <DropdownMenu.Root><DropdownMenu.Trigger asChild><button className="organization-switch-trigger" aria-label={`Switch organization: ${current.name}`} title="Switch organization"><ChevronsUpDownIcon size={16} aria-hidden="true" /></button></DropdownMenu.Trigger>
      <DropdownMenu.Portal><DropdownMenu.Content className="organization-menu" sideOffset={8} align="start" aria-label="Organizations">
        <DropdownMenu.RadioGroup value={selected} onValueChange={onSelect}>{organizations.map((organization) => <div className="organization-menu-entry" key={organization.id}>
          <DropdownMenu.RadioItem className="organization-menu-item" value={organization.id}><OrganizationIcon organization={organization} /><span>{organization.name}</span><DropdownMenu.ItemIndicator><CheckIcon size={16} aria-hidden="true" /></DropdownMenu.ItemIndicator></DropdownMenu.RadioItem>
          {onOpen && <div className="organization-menu-actions" aria-label={`${organization.name} actions`}>
            <DropdownMenu.Item className="organization-menu-action" aria-label={`Reporting chart for ${organization.name}`} title="Reporting chart" onSelect={() => onOpen(organization.id, 'chart')}><NetworkIcon aria-hidden="true" /></DropdownMenu.Item>
            {managerOrganizationIds.includes(organization.id) && <DropdownMenu.Item className="organization-menu-action" aria-label={`Organization settings for ${organization.name}`} title="Organization settings" onSelect={() => onOpen(organization.id, 'organization')}><SettingsIcon aria-hidden="true" /></DropdownMenu.Item>}
          </div>}
        </div>)}</DropdownMenu.RadioGroup>
        <DropdownMenu.Separator className="organization-menu-separator" />
        <DropdownMenu.Item className="organization-menu-item" onSelect={() => onSelect('__new__')}><PlusIcon size={16} aria-hidden="true" /><span>Create organization</span></DropdownMenu.Item>
      </DropdownMenu.Content></DropdownMenu.Portal>
    </DropdownMenu.Root>
  </div>;
}
