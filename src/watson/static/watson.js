(() => {
  setupThemeToggle();
  setupConfirmForms();
  setupDropzone();
  setupCodeDropzone();
  setupJobEvents();
  setupStudyLinks();
  setupProjectRows();

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
    input.addEventListener("change", () => updateSelection(true));

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
      updateSelection(true);
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

    function updateSelection(upload = false) {
      const files = Array.from(input.files || []);
      submit.disabled = files.length === 0;
      if (!files.length) {
        selection.textContent = "No files selected";
        return;
      }
      const names = files.slice(0, 3).map((file) => file.name).join(", ");
      const remainder = files.length > 3 ? ` and ${files.length - 3} more` : "";
      selection.textContent = `${files.length} file${files.length === 1 ? "" : "s"}: ${names}${remainder}`;
      if (upload) form.requestSubmit();
    }
  }

  function setupCodeDropzone() {
    const form = document.querySelector("[data-code-dropzone]");
    if (!form) return;
    const input = form.querySelector("#code-input"), folder = form.querySelector("#code-folder-input"), picker = form.querySelector("[data-code-picker]"), folderPicker = form.querySelector("[data-code-folder-picker]"), submit = form.querySelector("[data-code-submit]"), selection = form.querySelector("[data-code-selection]"), preview = form.querySelector("[data-code-preview]");
    let files = [];
    picker.addEventListener("click", () => input.click());
    input.addEventListener("change", () => setFiles(Array.from(input.files), true));
    folderPicker.addEventListener("click", () => folder.click());
    folder.addEventListener("change", () => setFiles(Array.from(folder.files), true));
    ["dragenter", "dragover"].forEach(name => form.addEventListener(name, event => { event.preventDefault(); }));
    form.addEventListener("drop", event => { event.preventDefault(); setFiles(Array.from(event.dataTransfer.files), true); });
    form.addEventListener("submit", event => {
      if (!files.length) { event.preventDefault(); return; }
      event.preventDefault(); const data = new FormData();
      files.forEach(file => { data.append("files", file); data.append("paths", file.webkitRelativePath || file.name); });
      submit.disabled = true; selection.textContent = `Uploading ${files.length} code file${files.length === 1 ? "" : "s"}…`;
      fetch(form.action, { method: "POST", body: data, redirect: "follow" })
        .then(response => { if (!response.ok) throw new Error("Upload failed."); window.location.assign(response.url); })
        .catch(() => { submit.disabled = false; selection.textContent = "Upload failed. Try again or check the file type and size."; });
    });
    function setFiles(next, upload) {
      files = next; submit.disabled = !files.length;
      const paths = files.map(file => file.webkitRelativePath || file.name);
      selection.textContent = files.length ? `${files.length} code file${files.length === 1 ? "" : "s"} selected` : "No code selected";
      preview.hidden = !files.length; preview.replaceChildren(...paths.slice(0, 12).map(path => { const item = document.createElement("li"); item.textContent = path; return item; }));
      if (paths.length > 12) { const item = document.createElement("li"); item.textContent = `…and ${paths.length - 12} more`; preview.append(item); }
      if (upload && files.length) form.requestSubmit();
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

  function setupProjectRows() {
    document.querySelectorAll(".project-row[data-project-href]").forEach((row) => {
      const openProject = () => {
        window.location.href = row.dataset.projectHref;
      };

      row.addEventListener("click", (event) => {
        if (event.target.closest("a, button, input, select, textarea")) return;
        openProject();
      });
      row.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        openProject();
      });
    });
  }
})();
