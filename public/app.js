const $ = (selector) => document.querySelector(selector);
const views = ["authView", "registerView", "employeeView", "adminView"];
const state = { session: null, stream: null, photo: null, location: null, office: null };
const periodNames = { datang: "Datang", siang: "Siang", pulang: "Pulang" };
const schedule = [
  { id: "datang", start: 7 * 60 + 30, end: 8 * 60 + 1 },
  { id: "siang", start: 12 * 60, end: 13 * 60 + 1 },
  { id: "pulang", start: 16 * 60, end: 18 * 60 + 1 },
];
const attendedPeriods = new Set();
let lastWitaDate = null;

function showNotice(message, kind = "error") {
  const notice = $("#notice");
  notice.textContent = message;
  notice.className = `notice ${kind}`;
  notice.scrollIntoView({ behavior: "smooth", block: "nearest" });
  window.setTimeout(() => notice.classList.add("hidden"), 6500);
}

async function api(path, options = {}) {
  if (window.location.protocol === "file:") {
    throw new Error("Mode preview hanya menampilkan tampilan. Jalankan server aplikasi untuk memakai absensi.");
  }
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(payload?.error || "Permintaan tidak dapat diproses.");
  return payload;
}

function setView(id) {
  if (id !== "employeeView") clearEmployeeDraft();
  views.forEach((view) => {
    const element = $(`#${view}`);
    const visible = view === id;
    element.classList.toggle("hidden", !visible);
    if (visible) {
      element.classList.remove("view-enter");
      requestAnimationFrame(() => element.classList.add("view-enter"));
    }
  });
  $("#logoutButton").classList.toggle("hidden", id === "authView" || id === "registerView");
}

function clearEmployeeDraft() {
  stopCamera();
  const preview = $("#photoPreview");
  if (preview.src.startsWith("blob:")) URL.revokeObjectURL(preview.src);
  preview.removeAttribute("src");
  preview.classList.add("hidden");
  $("#camera").classList.add("hidden");
  $("#cameraPlaceholder").classList.remove("hidden");
  $("#cameraButton").classList.remove("hidden");
  $("#retakeButton").classList.add("hidden");
  $("#photoSize").textContent = "";
  $("#locationStatus").textContent = "Lokasi akan diperiksa saat mengirim absensi";
  $("#locationStatus").classList.remove("location-ready");
  $("#locationButton").textContent = "Periksa lokasi";
  $("#submitAttendance").disabled = true;
  state.photo = null;
  state.location = null;
}

function updateClock() {
  const now = new Date();
  $("#clock").textContent = new Intl.DateTimeFormat("id-ID", {
    timeZone: "Asia/Makassar", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(now);
  $("#dateLabel").textContent = new Intl.DateTimeFormat("id-ID", {
    timeZone: "Asia/Makassar", weekday: "long", day: "numeric", month: "long", year: "numeric",
  }).format(now);
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Makassar", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(now);
  const timeParts = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  const currentDate = `${timeParts.year}-${timeParts.month}-${timeParts.day}`;
  if (lastWitaDate && currentDate !== lastWitaDate) resetDailyAttendance();
  lastWitaDate = currentDate;
  updateSchedule(Number(timeParts.hour) * 60 + Number(timeParts.minute));
}

function resetDailyAttendance() {
  attendedPeriods.clear();
  for (const period of schedule) {
    const step = $(`#step-${period.id}`);
    step.classList.remove("complete");
    const stepStatus = step.querySelector(".step-status");
    stepStatus.textContent = "Belum";
    stepStatus.classList.remove("done");
  }
  if (state.session?.role === "employee") loadToday();
}

function updateSchedule(currentMinute) {
  let activePeriod = null;
  let nextPeriod = null;

  for (const period of schedule) {
    const row = $(`[data-schedule="${period.id}"]`);
    const badge = $(`#schedule-${period.id}`);
    row.classList.remove("is-active", "is-past");
    const isActive = currentMinute >= period.start && currentMinute < period.end;
    if (isActive) {
      activePeriod = period;
      row.classList.add("is-active");
    }
    if (attendedPeriods.has(period.id)) {
      badge.textContent = "Tercatat";
      badge.className = "schedule-state is-recorded";
      continue;
    }
    if (isActive) {
      badge.textContent = "Sedang buka";
      badge.className = "schedule-state is-open";
    } else if (currentMinute < period.start) {
      if (!nextPeriod) nextPeriod = period;
      badge.textContent = `Dalam ${formatCountdown(period.start - currentMinute)}`;
      badge.className = "schedule-state is-upcoming";
    } else {
      badge.textContent = "Selesai";
      badge.className = "schedule-state is-past";
      row.classList.add("is-past");
    }
  }

  const message = $("#scheduleMessage");
  if (activePeriod) {
    message.textContent = `Absen ${periodNames[activePeriod.id].toLowerCase()} sedang dibuka — silakan bersiap.`;
  } else if (nextPeriod) {
    message.textContent = `Absen ${periodNames[nextPeriod.id].toLowerCase()} dibuka dalam ${formatCountdown(nextPeriod.start - currentMinute)}.`;
  } else {
    message.textContent = "Jadwal absensi hari ini telah selesai. Sampai jumpa besok!";
  }

  const windowTitle = $("#attendanceWindowTitle");
  const windowDetail = $("#attendanceWindowDetail");
  const windowState = $("#attendanceWindowState");
  const attendanceWindow = $("#attendanceWindow");
  attendanceWindow.classList.toggle("is-open", Boolean(activePeriod && !attendedPeriods.has(activePeriod.id)));
  if (activePeriod && attendedPeriods.has(activePeriod.id)) {
    windowTitle.textContent = `Absen ${periodNames[activePeriod.id].toLowerCase()} sudah tercatat`;
    windowDetail.textContent = "Terima kasih, kehadiranmu sudah tersimpan.";
    windowState.textContent = "Tercatat";
  } else if (activePeriod) {
    windowTitle.textContent = `Absen ${periodNames[activePeriod.id].toLowerCase()} sedang dibuka`;
    windowDetail.textContent = "Ambil foto dan kirim sebelum jadwal berakhir.";
    windowState.textContent = "Buka";
  } else if (nextPeriod) {
    windowTitle.textContent = `Absen ${periodNames[nextPeriod.id].toLowerCase()} belum dibuka`;
    windowDetail.textContent = `Jadwal berikutnya dalam ${formatCountdown(nextPeriod.start - currentMinute)}.`;
    windowState.textContent = "Menunggu";
  } else {
    windowTitle.textContent = "Jadwal absensi hari ini selesai";
    windowDetail.textContent = "Kamu bisa kembali untuk absensi besok.";
    windowState.textContent = "Selesai";
  }

  const progress = Math.min(100, Math.max(0, ((currentMinute - schedule[0].start) / (schedule[2].end - schedule[0].start)) * 100));
  $("#dayProgress").style.width = `${progress}%`;
}

function formatCountdown(totalMinutes) {
  if (totalMinutes < 60) return `${totalMinutes} mnt`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes ? `${hours}j ${minutes}m` : `${hours} jam`;
}

function formData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function loadSession() {
  const result = await api("/api/session");
  state.session = result;
  if (result.role === "employee") {
    const { employee } = result;
    $("#employeeName").textContent = employee.name.split(/\s+/)[0];
    $("#employeeMeta").textContent = `${employee.position} · NIP ${employee.nip}`;
    $("#avatar").textContent = employee.name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
    setView("employeeView");
    await Promise.all([loadToday(), loadOffice()]);
  } else if (result.role === "admin") {
    setView("adminView");
    await Promise.all([loadSettings(), loadOffice()]);
    setDefaultReportDates();
  } else setView("authView");
}

async function loadOffice() {
  const result = await api("/api/config");
  state.office = result.office;
}

async function loadToday() {
  try {
    const result = await api("/api/attendance/today");
    result.records.forEach((record) => {
      attendedPeriods.add(record.period);
      const scheduleBadge = $(`#schedule-${record.period}`);
      scheduleBadge.textContent = "Tercatat";
      scheduleBadge.className = "schedule-state is-recorded";
      const step = $(`#step-${record.period}`);
      step.classList.add("complete");
      step.querySelector(".step-status").textContent = "Tercatat";
      step.querySelector(".step-status").classList.add("done");
    });
  } catch (error) {
    if (error.message !== "Silakan masuk kembali.") showNotice(error.message);
  }
}

async function loadSettings() {
  const result = await api("/api/admin/settings");
  const office = result.office;
  const form = $("#settingsForm");
  if (office.configured) {
    form.elements.latitude.value = office.latitude;
    form.elements.longitude.value = office.longitude;
    form.elements.radius_m.value = office.radius_m;
  }
}

function setDefaultReportDates() {
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Makassar", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date());
  const dateParts = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  const today = `${dateParts.year}-${dateParts.month}-${dateParts.day}`;
  const form = $("#reportForm");
  form.elements.from.value = today;
  form.elements.to.value = today;
}

$("#showRegister").addEventListener("click", () => setView("registerView"));
$("#backToLogin").addEventListener("click", () => setView("authView"));

$("#loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify(formData(form)) });
    form.reset();
    await loadSession();
  } catch (error) { showNotice(error.message); }
  finally { button.disabled = false; }
});

$("#registerForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("/api/register", { method: "POST", body: JSON.stringify(formData(form)) });
    form.reset();
    await loadSession();
    showNotice("Akun berhasil dibuat. Selamat datang!", "success");
  } catch (error) { showNotice(error.message); }
  finally { button.disabled = false; }
});

$("#logoutButton").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", body: "{}" }); }
  catch (error) {
    showNotice(error.message);
    return;
  }
  state.session = null;
  setView("authView");
});

async function startCamera() {
  if (!navigator.mediaDevices?.getUserMedia) {
    showNotice("Kamera memerlukan HTTPS atau alamat localhost pada browser.");
    return;
  }
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 960 } } });
    $("#camera").srcObject = state.stream;
    $("#camera").classList.remove("hidden");
    $("#cameraPlaceholder").classList.add("hidden");
    $("#photoPreview").classList.add("hidden");
    $("#cameraButton").classList.add("hidden");
    $("#retakeButton").classList.add("hidden");
    $("#photoSize").textContent = "Ketuk kamera untuk mengambil foto";
    $("#submitAttendance").disabled = true;
  } catch (error) {
    showNotice(error.name === "NotAllowedError" ? "Izin kamera ditolak. Izinkan akses kamera di pengaturan browser." : "Kamera tidak dapat dibuka. Pastikan kamera tersedia dan tidak sedang digunakan.");
  }
}

function stopCamera() {
  state.stream?.getTracks().forEach((track) => track.stop());
  state.stream = null;
  $("#camera").srcObject = null;
}

async function compressPhoto(blob) {
  const image = await createImageBitmap(blob);
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d", { alpha: false });
  let scale = Math.min(1, 1600 / Math.max(image.width, image.height));
  let quality = 0.84;
  let result;
  for (let attempt = 0; attempt < 24; attempt += 1) {
    canvas.width = Math.max(1, Math.round(image.width * scale));
    canvas.height = Math.max(1, Math.round(image.height * scale));
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    result = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
    if (!result) throw new Error("Foto gagal diproses oleh browser.");
    if (result.size <= 200 * 1024) break;
    if (quality > 0.46) quality -= 0.09;
    else scale *= 0.82;
  }
  image.close();
  if (!result || result.size > 200 * 1024) throw new Error("Ukuran foto tidak dapat dikurangi hingga 200 KB. Coba ambil ulang.");
  return result;
}

$("#cameraButton").addEventListener("click", startCamera);
$("#retakeButton").addEventListener("click", startCamera);
$("#cameraBox").addEventListener("click", async (event) => {
  if (!state.stream || event.target.closest("button")) return;
  const video = $("#camera");
  if (!video.videoWidth) return;
  try {
    const raw = await new Promise((resolve, reject) => {
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const context = canvas.getContext("2d");
      context.translate(canvas.width, 0);
      context.scale(-1, 1);
      context.drawImage(video, 0, 0);
      canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("Foto gagal diambil.")), "image/jpeg", 0.94);
    });
    state.photo = await compressPhoto(raw);
    stopCamera();
    $("#photoPreview").src = URL.createObjectURL(state.photo);
    $("#photoPreview").classList.remove("hidden");
    $("#camera").classList.add("hidden");
    $("#retakeButton").classList.remove("hidden");
    $("#photoSize").textContent = `${(state.photo.size / 1024).toFixed(0)} KB`;
    updateSubmitButton();
  } catch (error) { showNotice(error.message); }
});

function updateSubmitButton() {
  $("#submitAttendance").disabled = !state.photo || !state.location;
}

async function locate() {
  if (!navigator.geolocation) {
    showNotice("Browser ini tidak mendukung pemeriksaan lokasi.");
    return;
  }
  $("#locationStatus").textContent = "Memeriksa lokasi perangkat…";
  $("#locationButton").disabled = true;
  navigator.geolocation.getCurrentPosition(
    (position) => {
      state.location = { latitude: position.coords.latitude, longitude: position.coords.longitude };
      const accuracy = Math.round(position.coords.accuracy);
      $("#locationStatus").textContent = `Lokasi diperoleh · akurasi sekitar ${accuracy} m`;
      $("#locationStatus").classList.add("location-ready");
      $("#locationButton").textContent = "Perbarui";
      $("#locationButton").disabled = false;
      updateSubmitButton();
    },
    (error) => {
      state.location = null;
      $("#locationStatus").textContent = error.code === error.PERMISSION_DENIED ? "Izin lokasi ditolak. Izinkan akses lokasi di browser." : "Lokasi tidak tersedia. Coba lagi di area terbuka.";
      $("#locationButton").disabled = false;
      updateSubmitButton();
    },
    { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 },
  );
}

$("#locationButton").addEventListener("click", locate);

$("#submitAttendance").addEventListener("click", async () => {
  if (!state.photo || !state.location) return;
  const button = $("#submitAttendance");
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span> Memeriksa absensi…';
  try {
    const photo = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("Foto gagal dibaca."));
      reader.readAsDataURL(state.photo);
    });
    const result = await api("/api/attendance", { method: "POST", body: JSON.stringify({ ...state.location, photo }) });
    showNotice(`${result.label} berhasil dicatat. Jarak dari kantor ${result.distance_m} m.`, "success");
    clearEmployeeDraft();
    ["datang", "siang", "pulang"].includes(result.period) && await loadToday();
  } catch (error) {
    showNotice(error.message);
    if (error.message === "Silakan masuk kembali.") await loadSession();
  } finally {
    button.innerHTML = 'Kirim absensi <span>→</span>';
    button.disabled = !state.photo || !state.location;
  }
});

$("#settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("/api/admin/settings", { method: "POST", body: JSON.stringify(formData(event.currentTarget)) });
    await loadOffice();
    showNotice("Lokasi kantor berhasil disimpan.", "success");
  } catch (error) { showNotice(error.message); }
  finally { button.disabled = false; }
});

function dateQuery(form) {
  const values = formData(form);
  if (!values.from || !values.to) throw new Error("Pilih tanggal awal dan akhir terlebih dahulu.");
  if (values.to < values.from) throw new Error("Tanggal akhir tidak boleh sebelum tanggal awal.");
  return new URLSearchParams(values).toString();
}

$("#reportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const tbody = $("#reportRows");
  tbody.innerHTML = '<tr><td colspan="5" class="empty-state">Memuat rekap…</td></tr>';
  try {
    const query = dateQuery(event.currentTarget);
    const result = await api(`/api/admin/report?${query}`);
    $("#reportSummary").textContent = `${result.records.length} catatan ditemukan`;
    if (!result.records.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty-state">Belum ada catatan absensi pada rentang tanggal ini.</td></tr>';
      return;
    }
    tbody.replaceChildren(...result.records.map((record) => {
      const row = document.createElement("tr");
      const photoCell = document.createElement("td");
      const image = document.createElement("img");
      image.className = "report-photo";
      image.src = `/api/admin/photo/${record.id}`;
      image.alt = `Foto ${record.name}`;
      photoCell.append(image);
      const employeeCell = document.createElement("td");
      employeeCell.innerHTML = `<strong></strong><small></small>`;
      employeeCell.querySelector("strong").textContent = record.name;
      employeeCell.querySelector("small").textContent = `${record.position} · ${record.nip}`;
      const timeCell = document.createElement("td");
      timeCell.innerHTML = `<strong></strong><small></small>`;
      timeCell.querySelector("strong").textContent = record.attendance_date;
      timeCell.querySelector("small").textContent = new Intl.DateTimeFormat("id-ID", { timeZone: "Asia/Makassar", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(record.taken_at));
      const periodCell = document.createElement("td");
      periodCell.textContent = periodNames[record.period];
      const distanceCell = document.createElement("td");
      distanceCell.textContent = `${Math.round(record.distance_m)} m`;
      row.append(photoCell, employeeCell, timeCell, periodCell, distanceCell);
      return row;
    }));
  } catch (error) { showNotice(error.message); }
});

$("#exportButton").addEventListener("click", async () => {
  try {
    const query = dateQuery($("#reportForm"));
    const response = await fetch(`/api/admin/export?${query}`, { credentials: "same-origin" });
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.error || "Rekap gagal diunduh.");
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = `rekap-absensi-${$("#reportForm").elements.from.value}-${$("#reportForm").elements.to.value}.zip`;
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) { showNotice(error.message); }
});

updateClock();
window.setInterval(updateClock, 1000);
if (window.location.protocol === "file:") {
  setView("authView");
  showNotice("Mode preview tampilan aktif. Jalankan server untuk masuk, memakai kamera, dan mengirim absensi.", "success");
} else {
  loadSession().catch((error) => showNotice(error.message));
}
if ("serviceWorker" in navigator && window.location.protocol !== "file:") {
  window.addEventListener("load", () => navigator.serviceWorker.register("./sw.js").catch(() => showNotice("Aplikasi tidak dapat menyiapkan fitur PWA. Muat ulang halaman untuk mencoba lagi.")));
}
