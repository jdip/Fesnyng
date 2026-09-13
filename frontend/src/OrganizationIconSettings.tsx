import { useEffect, useRef, useState } from 'react';
import { OrganizationIcon } from './OrganizationIcon';
import { api, errorMessage, type Organization } from './workspace-api';

type Icon = NonNullable<Organization['icon']>;
type OrganizationWithIcon = Organization;
type IconState = { organization: string; saved: Icon | null; draft: Icon | null; loading: boolean; busy: boolean; reading: boolean; error: string; notice: string };
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);
const MAX_IMAGE_BYTES = 2 * 1024 * 1024;

function organizationWithIcon(value: unknown): OrganizationWithIcon {
  if (!value || typeof value !== 'object') {
    throw new Error('Could not read organization identity.');
  }
  const record = value as Record<string, unknown>;
  if (typeof record.id !== 'string' || typeof record.name !== 'string') throw new Error('Could not read organization identity.');
  const icon = 'icon' in record ? record.icon : null;
  const iconRecord = icon && typeof icon === 'object' ? icon as Record<string, unknown> : undefined;
  if (icon !== null && (!iconRecord || (iconRecord.kind !== 'emoji' && iconRecord.kind !== 'image') || typeof iconRecord.value !== 'string')) {
    throw new Error('Could not read organization identity.');
  }
  return { id: record.id, name: record.name, icon: icon as Icon | null };
}

function validImageDataUrl(value: string) {
  return /^data:image\/(png|jpeg|webp);base64,/i.test(value);
}

/** Manager controls for a compact, organization-wide identity mark. */
export function OrganizationIconSettings({ organization, csrf, onSaved }: {
  organization: string;
  csrf: string;
  onSaved: (updated: Organization) => void;
}) {
  const [state, setState] = useState<IconState>({ organization, saved: null, draft: null, loading: true, busy: false, reading: false, error: '', notice: '' });
  const [reload, setReload] = useState(0);
  const activeOrganization = useRef(organization);
  const saveController = useRef<AbortController | undefined>(undefined);
  const readerVersion = useRef(0);
  const visible = state.organization === organization;
  const current = visible ? state : { organization, saved: null, draft: null, loading: true, busy: false, reading: false, error: '', notice: '' };

  useEffect(() => {
    activeOrganization.current = organization;
    const controller = new AbortController();
    void api<unknown>(`/organizations/${encodeURIComponent(organization)}`, { signal: controller.signal }).then((value) => {
      const updated = organizationWithIcon(value);
      if (controller.signal.aborted || activeOrganization.current !== organization || updated.id !== organization) return;
      setState({ organization, saved: updated.icon ?? null, draft: updated.icon ?? null, loading: false, busy: false, reading: false, error: '', notice: '' });
    }).catch((cause: unknown) => {
      if (controller.signal.aborted || activeOrganization.current !== organization) return;
      setState({ organization, saved: null, draft: null, loading: false, busy: false, reading: false, error: errorMessage(cause), notice: '' });
    });
    return () => {
      controller.abort();
      saveController.current?.abort();
      readerVersion.current += 1;
    };
  }, [organization, reload]);

  const setDraft = (draft: Icon | null) => {
    readerVersion.current += 1;
    setState((previous) => previous.organization === organization
      ? { ...previous, draft, reading: false, error: '', notice: '' }
      : previous);
  };

  const loadImage = (file: File | undefined) => {
    const version = ++readerVersion.current;
    if (!file) {
      setState((previous) => previous.organization === organization ? { ...previous, reading: false } : previous);
      return;
    }
    if (!IMAGE_TYPES.has(file.type)) {
      setState((previous) => previous.organization === organization ? { ...previous, reading: false, error: 'Choose a PNG, JPEG, or WebP image.', notice: '' } : previous);
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      setState((previous) => previous.organization === organization ? { ...previous, reading: false, error: 'Choose an image no larger than 2 MiB.', notice: '' } : previous);
      return;
    }
    setState((previous) => previous.organization === organization ? { ...previous, reading: true, error: '', notice: '' } : previous);
    const reader = new FileReader();
    reader.onload = () => {
      const value = reader.result;
      if (version !== readerVersion.current || activeOrganization.current !== organization) return;
      if (typeof value !== 'string' || !validImageDataUrl(value)) {
        setState((previous) => previous.organization === organization ? { ...previous, reading: false, error: 'The image could not be read.', notice: '' } : previous);
        return;
      }
      setState((previous) => previous.organization === organization
        ? { ...previous, draft: { kind: 'image', value }, reading: false, error: '', notice: '' }
        : previous);
    };
    reader.onerror = () => {
      if (version !== readerVersion.current || activeOrganization.current !== organization) return;
      setState((previous) => previous.organization === organization ? { ...previous, reading: false, error: 'The image could not be read.', notice: '' } : previous);
    };
    reader.readAsDataURL(file);
  };

  const save = async () => {
    if (current.loading || current.busy || current.reading) return;
    const controller = new AbortController();
    saveController.current?.abort();
    saveController.current = controller;
    readerVersion.current += 1;
    const draft = current.draft;
    setState((previous) => previous.organization === organization ? { ...previous, busy: true, error: '', notice: '' } : previous);
    try {
      const updated = organizationWithIcon(await api<unknown>(`/organizations/${encodeURIComponent(organization)}/icon`, {
        method: 'PUT', csrf, signal: controller.signal, body: { icon: draft },
      }));
      if (controller.signal.aborted || activeOrganization.current !== organization || updated.id !== organization) return;
      setState({ organization, saved: updated.icon ?? null, draft: updated.icon ?? null, loading: false, busy: false, reading: false, error: '', notice: 'Organization icon saved.' });
      onSaved(updated);
    } catch (cause) {
      if (controller.signal.aborted || activeOrganization.current !== organization) return;
      setState((previous) => previous.organization === organization ? { ...previous, busy: false, error: errorMessage(cause), notice: '' } : previous);
    } finally {
      if (saveController.current === controller) saveController.current = undefined;
    }
  };

  if (!visible || current.loading) return <section className="app-panel" aria-label="Organization identity"><h3>Organization identity</h3><p role="status">Loading organization identity…</p></section>;
  const emoji = current.draft?.kind === 'emoji' ? current.draft.value : '';
  return <section className="app-panel app-form" aria-label="Organization identity"><h3>Organization identity</h3><p className="muted">Choose an emoji or a small image for this organization.</p>
    {current.draft && <p role="img" aria-label="Organization icon preview"><OrganizationIcon organization={{ id: organization, name: 'Organization', icon: current.draft }} /></p>}
    <label>Organization emoji<input className="app-input" aria-label="Organization emoji" value={emoji} maxLength={16} disabled={current.busy} onChange={(event) => setDraft(event.target.value ? { kind: 'emoji', value: event.target.value } : null)} /></label>
    <label>Organization image<input className="app-input" aria-label="Organization image" type="file" accept="image/png,image/jpeg,image/webp" disabled={current.busy} onChange={(event) => loadImage(event.currentTarget.files?.[0])} /></label>
    <div className="app-actions"><button type="button" className="app-button primary" disabled={current.busy || current.reading} onClick={() => { void save(); }}>{current.busy ? 'Saving…' : 'Save icon'}</button><button type="button" className="app-button" disabled={current.busy || current.draft === null} onClick={() => setDraft(null)}>Clear icon</button><button type="button" className="app-button quiet" disabled={current.busy} onClick={() => setDraft(current.saved)}>Reset</button></div>
    {current.reading && <p role="status" className="muted">Reading image…</p>}
    {current.draft === null && current.saved !== null && <p className="muted">Save icon to clear the organization icon.</p>}
    {current.notice && <p className="app-notice" role="status">{current.notice}</p>}
    {current.error && <p className="app-error" role="alert">{current.error}</p>}
    {current.error && <button type="button" className="app-button quiet" disabled={current.busy || current.reading} onClick={() => {
      setState((previous) => previous.organization === organization ? { ...previous, loading: true, reading: false } : previous);
      setReload((value) => value + 1);
    }}>Retry identity</button>}
  </section>;
}
