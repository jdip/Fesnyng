import { useState } from 'react';
import type { Organization } from './workspace-api';

export function OrganizationIcon({ organization }: { organization: Organization }) {
  const [failedImage, setFailedImage] = useState<string>();
  const icon = organization.icon;
  const initials = organization.name.trim().split(/\s+/u).slice(0, 2).map((part) => Array.from(part)[0] ?? '').join('').toLocaleUpperCase() || '?';
  return <span className="organization-icon" aria-hidden="true">{icon?.kind === 'image' && /^data:image\/(png|jpeg|webp);base64,/i.test(icon.value) && failedImage !== icon.value
    ? <img src={icon.value} alt="" onError={() => setFailedImage(icon.value)} />
    : icon?.kind === 'emoji' ? icon.value : initials}</span>;
}
