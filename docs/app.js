import { clusterItems, featuresFromBitmap } from "./sorter.js"
import { heicTo, isHeic } from "./vendor/heic-to.js"

const drop = document.querySelector("#drop")
const picker = document.querySelector("#picker")
const download = document.querySelector("#download")
const again = document.querySelector("#again")
const status = document.querySelector("#status")
const grid = document.querySelector("#grid")

const IMAGE = /\.(jpe?g|png|heic|heif|webp|gif|bmp|tiff?)$/i
let latest = null

drop.addEventListener("click", () => picker.click())
drop.addEventListener("dragover", (event) => {
  event.preventDefault()
  drop.classList.add("over")
})
drop.addEventListener("dragleave", () => drop.classList.remove("over"))
function readEntries(reader) {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject))
}

async function walkEntry(entry, files) {
  if (entry.fullPath && entry.fullPath.split("/").includes("sorted")) return
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject))
    files.push(file)
    return
  }
  if (!entry.isDirectory) return
  const reader = entry.createReader()
  while (true) {
    const batch = await readEntries(reader)
    if (!batch.length) break
    for (const child of batch) await walkEntry(child, files)
  }
}

drop.addEventListener("drop", async (event) => {
  event.preventDefault()
  drop.classList.remove("over")
  const files = []
  const entries = [...event.dataTransfer.items]
    .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter(Boolean)
  if (entries.length) {
    for (const entry of entries) await walkEntry(entry, files)
  } else {
    files.push(...event.dataTransfer.files)
  }
  sortFiles(files.filter(Boolean))
})
picker.addEventListener("change", () => sortFiles([...picker.files]))
again.addEventListener("click", () => {
  latest = null
  grid.innerHTML = ""
  status.textContent = ""
  download.hidden = true
  again.hidden = true
  picker.value = ""
})
download.addEventListener("click", () => {
  if (latest) saveZip(latest)
})

function usable(file) {
  const path = file.webkitRelativePath || file.name
  if (!IMAGE.test(file.name)) return false
  if (path.split("/").includes("sorted")) return false
  if (file.name.startsWith(".")) return false
  return true
}

async function sha1(file) {
  const digest = await crypto.subtle.digest("SHA-1", await file.arrayBuffer())
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("")
}

async function captureTime(file) {
  let tags = null
  try {
    tags = await exifr.parse(file, {
      pick: ["DateTimeOriginal", "SubSecTimeOriginal", "SubSecTimeDigitized", "DateTime"],
      reviveValues: false,
    })
  } catch {
    return null
  }
  if (!tags) return null
  const sub = String(tags.SubSecTimeOriginal || tags.SubSecTimeDigitized || "")
  const raw = tags.DateTimeOriginal || tags.DateTime
  if (raw instanceof Date && !Number.isNaN(raw.getTime())) {
    if (sub && raw.getMilliseconds() === 0) {
      return raw.getTime() + Number(sub.padEnd(3, "0").slice(0, 3))
    }
    return raw.getTime()
  }
  if (typeof raw === "string") {
    const match = raw.match(/(\d{4}):(\d{2}):(\d{2})[ ](\d{2}):(\d{2}):(\d{2})/)
    if (!match) return null
    const frac = (sub || "0").padEnd(3, "0").slice(0, 3)
    return Date.UTC(+match[1], +match[2] - 1, +match[3], +match[4], +match[5], +match[6], +frac)
  }
  return null
}

async function bitmapOf(file) {
  let blob = file
  if (await isHeic(file)) {
    blob = await heicTo({ blob: file, type: "image/jpeg", quality: 0.92 })
  }
  return createImageBitmap(blob, { imageOrientation: "from-image" })
}

async function sortFiles(files) {
  const chosen = files.filter(usable)
  grid.innerHTML = ""
  download.hidden = true
  again.hidden = true
  if (!chosen.length) {
    status.textContent = "No photos found in that folder."
    return
  }
  status.textContent = `Reading 0 of ${chosen.length}`
  const seen = new Set()
  const items = []
  const skipped = []
  for (let index = 0; index < chosen.length; index++) {
    const file = chosen[index]
    status.textContent = `Reading ${index + 1} of ${chosen.length}`
    try {
      const hash = await sha1(file)
      if (seen.has(hash)) continue
      seen.add(hash)
      const time = await captureTime(file)
      const bitmap = await bitmapOf(file)
      const features = featuresFromBitmap(bitmap)
      bitmap.close()
      items.push({
        id: hash.slice(0, 12),
        name: file.name,
        time,
        file,
        preview: URL.createObjectURL(file),
        ...features,
      })
    } catch (error) {
      skipped.push(file.name)
    }
  }
  if (!items.length) {
    status.textContent = "None of those files could be read as photos."
    return
  }
  const groups = clusterItems(items)
  latest = groups
  render(groups, skipped)
  download.hidden = false
  again.hidden = false
}

function render(groups, skipped) {
  const photos = groups.reduce((sum, group) => sum + group.length, 0)
  status.textContent = `${groups.length} products from ${photos} photos. Each product has at most 5.`
  if (skipped.length) {
    const note = document.createElement("p")
    note.className = "warn"
    note.textContent = `Skipped ${skipped.length} file${skipped.length === 1 ? "" : "s"} that could not be read: ${skipped.slice(0, 6).join(", ")}`
    grid.append(note)
  }
  groups.forEach((group, index) => {
    const card = document.createElement("article")
    card.className = "card"
    const title = document.createElement("h2")
    const label = String(index + 1).padStart(2, "0")
    title.textContent = `${label} · ${group.length} photo${group.length === 1 ? "" : "s"}`
    const thumbs = document.createElement("div")
    thumbs.className = "thumbs"
    for (const item of group) {
      const img = document.createElement("img")
      img.src = item.preview
      img.alt = item.name
      thumbs.append(img)
    }
    const names = document.createElement("p")
    names.className = "names"
    names.textContent = group.map((item) => item.name).join(", ")
    card.append(title, thumbs, names)
    grid.append(card)
  })
}

function saveZip(groups) {
  const files = {}
  groups.forEach((group, index) => {
    const folder = String(index + 1).padStart(2, "0")
    const lines = [`Product ${folder}`, ...group.map((item) => item.name)]
    files[`sorted/${folder}/listing.txt`] = fflate.strToU8(lines.join("\n") + "\n")
  })
  status.textContent = "Packing the zip…"
  const reads = []
  groups.forEach((group, index) => {
    const folder = String(index + 1).padStart(2, "0")
    const used = new Set()
    group.forEach((item, photo) => {
      let name = item.name
      if (used.has(name)) name = `${photo + 1}-${name}`
      used.add(name)
      reads.push(
        item.file.arrayBuffer().then((buffer) => {
          files[`sorted/${folder}/${name}`] = new Uint8Array(buffer)
        })
      )
    })
  })
  Promise.all(reads).then(() => {
    const zipped = fflate.zipSync(files, { level: 0 })
    const blob = new Blob([zipped], { type: "application/zip" })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = "sorted.zip"
    link.click()
    URL.revokeObjectURL(url)
    status.textContent = `${groups.length} products packed. Unzip sorted.zip and you get a sorted folder.`
  })
}
