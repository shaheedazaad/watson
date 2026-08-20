(() => {
  setupThemeToggle();
  setupConfirmForms();
  setupDropzone();
  setupJobEvents();
  setupStudyLinks();

  function setupConfirmForms() {
    document.querySelectorAll("form[data-confirm]").forEach((form) => {
      form.addEventListener("submit", (event) => {
        if (!window.confirm(form.dataset.confirm)) event.preventDefault();
      });
    });
  }

  function setupThemeToggle() {
    const toggle = document.querySelector("[data-theme-toggle]");
    if (!toggle) return;
    toggle.addEventListener("click", () => {
      const dark = !document.documentElement.classList.contains("dark");
      document.documentElement.classList.toggle("dark", dark);
      localStorage.setItem("watson-theme", dark ? "dark" : "light");
    });
  }

  function setupDropzone() {
    const form = document.querySelector("[data-dropzone]");
    if (!form) return;

    const input = form.querySelector('input[type="file"]');
    const picker = form.querySelector("[data-file-picker]");
    const submit = form.querySelector("[data-upload-submit]");
    const selection = form.querySelector("[data-file-selection]");
    if (!input || !picker || !submit || !selection) return;

    picker.addEventListener("click", () => input.click());
    input.addEventListener("change", updateSelection);

    for (const eventName of ["dragenter", "dragover"]) {
      form.addEventListener(eventName, (event) => {
        event.preventDefault();
        if (!input.disabled) form.classList.add("is-dragging");
      });
    }
    for (const eventName of ["dragleave", "dragend"]) {
      form.addEventListener(eventName, () => form.classList.remove("is-dragging"));
    }
    form.addEventListener("drop", (event) => {
      event.preventDefault();
      form.classList.remove("is-dragging");
      if (input.disabled || !event.dataTransfer?.files.length) return;
      try {
        input.files = event.dataTransfer.files;
      } catch (_error) {
        selection.textContent = "This browser cannot attach dropped files. Use Add files instead.";
        return;
      }
      updateSelection();
    });
    form.addEventListener("submit", (event) => {
      if (!input.files?.length) {
        event.preventDefault();
        input.click();
        return;
      }
      submit.disabled = true;
      picker.disabled = true;
      selection.textContent = `Uploading ${input.files.length} file${input.files.length === 1 ? "" : "s"}…`;
    });

    function updateSelection() {
      const files = Array.from(input.files || []);
      submit.disabled = files.length === 0;
      if (!files.length) {
        selection.textContent = "No files selected";
        return;
      }
      const names = files.slice(0, 3).map((file) => file.name).join(", ");
      const remainder = files.length > 3 ? ` and ${files.length - 3} more` : "";
      selection.textContent = `${files.length} file${files.length === 1 ? "" : "s"}: ${names}${remainder}`;
    }
  }

  function setupJobEvents() {
    const panel = document.querySelector("[data-job-events]");
    if (!panel || !window.EventSource) return;
    const source = new EventSource(panel.dataset.jobEvents);
    const message = document.getElementById("run-message");
    source.addEventListener("progress", (event) => {
      const update = JSON.parse(event.data);
      if (message) message.textContent = update.message;
    });
    source.addEventListener("terminal", () => {
      source.close();
      window.location.reload();
    });
  }

  function setupStudyLinks() {
    const openStudy = (hash) => {
      if (!hash?.startsWith("#study-")) return;
      const study = document.querySelector(hash);
      if (study instanceof HTMLDetailsElement) study.open = true;
    };
    document.querySelectorAll('.study-index a[href^="#study-"]').forEach((link) => {
      link.addEventListener("click", () => openStudy(link.hash));
    });
    openStudy(window.location.hash);
  }
})();
