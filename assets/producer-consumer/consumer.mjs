// Example view adapter: the application owns the actual DOM/state/request layer.
export function renderPreview(payload, view) {
  const count = payload?.station_count;
  if (payload?.kind !== 'preview' || !Number.isSafeInteger(count) || count < 0) {
    view.count.textContent = 'Invalid preview';
    view.confirm.disabled = true;
    return;
  }
  view.count.textContent = count === 0 ? 'No stations' : `${count} stations`;
  view.confirm.disabled = count === 0;
}
