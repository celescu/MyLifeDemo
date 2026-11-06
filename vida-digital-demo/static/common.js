function formatearDetalle(data) {
  const detalle = data && data.detail;
  if (Array.isArray(detalle)) {
    return detalle.map(err => err.msg || JSON.stringify(err)).join(' · ');
  }
  return detalle || 'Error.';
}
