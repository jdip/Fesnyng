import { useEffect, useEffectEvent, useRef } from 'react';

/** A read receipt starts only once the rendered content's tail is actually visible. */
export function ViewedContent({ identity, onView }: { identity: string; onView: () => void | Promise<boolean> }) {
  const tail = useRef<HTMLSpanElement>(null);
  const viewed = useEffectEvent(onView);
  useEffect(() => {
    const node = tail.current;
    if (!node || typeof IntersectionObserver === 'undefined') return;
    let intersecting = false;
    let acknowledged = false;
    let disposed = false;
    const check = () => {
      if (!disposed && intersecting && document.visibilityState === 'visible' && !acknowledged) {
        acknowledged = true;
        const receipt = viewed();
        if (receipt) void receipt.then((saved) => { if (!saved) acknowledged = false; }).catch(() => { acknowledged = false; });
      }
    };
    const viewport = node.closest('[data-slot="aui_thread-viewport"]');
    const footer = viewport?.querySelector('.aui-thread-viewport-footer');
    let observer: IntersectionObserver;
    const observe = () => {
      if (disposed) return;
      observer?.disconnect();
      intersecting = false;
      observer = new IntersectionObserver((entries) => {
        intersecting = entries.some((entry) => entry.isIntersecting);
        check();
      }, { root: viewport, rootMargin: `0px 0px -${footer?.getBoundingClientRect().height ?? 0}px 0px` });
      observer.observe(node);
    };
    observe();
    const resize = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(observe);
    if (footer) resize?.observe(footer);
    document.addEventListener('visibilitychange', check);
    window.addEventListener('focus', check);
    return () => {
      disposed = true;
      observer.disconnect();
      resize?.disconnect();
      document.removeEventListener('visibilitychange', check);
      window.removeEventListener('focus', check);
    };
  }, [identity]);
  return <span ref={tail} aria-hidden="true" style={{ display: 'block', height: 1 }} />;
}
