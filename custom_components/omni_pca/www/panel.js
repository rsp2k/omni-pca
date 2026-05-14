// omni-pca side panel — stub until Phase C frontend lands.
class OmniPanelPrograms extends HTMLElement {
  set hass(hass) {
    if (!this._rendered) {
      this.innerHTML = `
        <style>
          :host, .root { display: block; padding: 24px; font-family: sans-serif; }
          h1 { font-size: 1.25rem; margin: 0 0 8px; }
          p  { color: #666; margin: 0; }
        </style>
        <div class="root">
          <h1>Omni Programs</h1>
          <p>Frontend bundle not yet installed.
             Phase C of the program viewer will populate this panel.</p>
        </div>`;
      this._rendered = true;
    }
  }
}
customElements.define('omni-panel-programs', OmniPanelPrograms);
