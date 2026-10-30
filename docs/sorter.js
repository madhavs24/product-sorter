/**
 * Same grouping rules as src/group_cmd.py.
 * Photos stay on the device. This file never sends them anywhere.
 */

export const PAUSE_SECONDS = 90
export const MAX_PHOTOS = 5

function parseTime(value) {
  if (value == null || value === "") return null
  if (typeof value === "number" && Number.isFinite(value)) return value
  const match = String(value).match(/(\d+)-(\d+)-(\d+)[T ](\d+):(\d+):(\d+)(?:\.(\d+))?/)
  if (!match) return null
  const frac = (match[7] || "0").padEnd(3, "0").slice(0, 3)
  return Date.UTC(+match[1], +match[2] - 1, +match[3], +match[4], +match[5], +match[6], +frac)
}

function hamming(a, b) {
  if (!a || !b || a.length !== b.length) return 64
  let bits = 0
  for (let i = 0; i < a.length; i++) {
    let x = parseInt(a[i], 16) ^ parseInt(b[i], 16)
    while (x) {
      bits += x & 1
      x >>= 1
    }
  }
  return bits
}

function dot(a, b) {
  let sum = 0
  for (let i = 0; i < a.length; i++) sum += a[i] * b[i]
  return sum
}

function norm(v) {
  const n = Math.sqrt(dot(v, v)) || 1e-9
  return v.map((x) => x / (n + 1e-9))
}

function l2(a, b) {
  let sum = 0
  for (let i = 0; i < a.length; i++) {
    const d = a[i] - b[i]
    sum += d * d
  }
  return Math.sqrt(sum)
}

function histogram(values) {
  const bins = [0, 0, 0, 0, 0, 0, 0, 0]
  for (let i = 0; i < values.length; i++) {
    let bin = Math.floor(values[i] * 8)
    if (bin < 0) bin = 0
    if (bin > 7) bin = 7
    bins[bin]++
  }
  const width = values.length * 0.125
  return bins.map((count) => count / width)
}

function sample(imageData, x0, y0, x1, y1) {
  const { data, width } = imageData
  const values = []
  for (let y = y0; y < y1; y++) {
    for (let x = x0; x < x1; x++) {
      const i = (y * width + x) * 4
      values.push([data[i] / 255, data[i + 1] / 255, data[i + 2] / 255])
    }
  }
  return values
}

function channelValues(pixels, channel) {
  const out = new Array(pixels.length)
  for (let i = 0; i < pixels.length; i++) out[i] = pixels[i][channel]
  return out
}

function mean3(pixels) {
  let r = 0
  let g = 0
  let b = 0
  for (const p of pixels) {
    r += p[0]
    g += p[1]
    b += p[2]
  }
  const n = pixels.length || 1
  return [r / n, g / n, b / n]
}

/** Center color and how much the border differs from the center. */
export function shotCues(imageData) {
  const center = sample(imageData, 16, 16, 64, 64)
  const vector = norm([
    ...histogram(channelValues(center, 0)),
    ...histogram(channelValues(center, 1)),
    ...histogram(channelValues(center, 2)),
  ])
  const border = [
    ...sample(imageData, 0, 0, 80, 8),
    ...sample(imageData, 0, 72, 80, 80),
    ...sample(imageData, 0, 0, 8, 80),
    ...sample(imageData, 72, 0, 80, 80),
  ]
  return { v: vector, border: l2(mean3(border), mean3(center)) }
}

export function colorSignature(imageData) {
  const all = sample(imageData, 0, 0, 48, 48)
  const vector = norm([
    ...histogram(channelValues(all, 0)),
    ...histogram(channelValues(all, 1)),
    ...histogram(channelValues(all, 2)),
  ])
  const center = sample(imageData, 12, 12, 36, 36)
  let blue = 0
  for (const p of center) {
    if (p[2] > p[0] + 0.08 && p[2] > p[1] + 0.02 && p[2] > 0.25) blue++
  }
  return { v: vector, mean: mean3(all), blue: blue / center.length }
}

function resize(bitmap, size) {
  const canvas = document.createElement("canvas")
  canvas.width = size
  canvas.height = size
  const ctx = canvas.getContext("2d", { willReadFrequently: true })
  ctx.imageSmoothingEnabled = true
  ctx.imageSmoothingQuality = "high"
  ctx.drawImage(bitmap, 0, 0, size, size)
  return ctx.getImageData(0, 0, size, size)
}

function dct2(pixels, n) {
  const row = new Float64Array(n * n)
  const out = new Float64Array(n * n)
  const factor = Math.PI / (2 * n)
  for (let y = 0; y < n; y++) {
    for (let k = 0; k < n; k++) {
      let sum = 0
      for (let x = 0; x < n; x++) sum += pixels[y * n + x] * Math.cos((2 * x + 1) * k * factor)
      row[y * n + k] = sum * 2
    }
  }
  for (let x = 0; x < n; x++) {
    for (let k = 0; k < n; k++) {
      let sum = 0
      for (let y = 0; y < n; y++) sum += row[y * n + x] * Math.cos((2 * y + 1) * k * factor)
      out[k * n + x] = sum * 2
    }
  }
  return out
}

/** 64-bit pHash hex, matching imagehash.phash on the same 32×32 gray pixels. */
export function phashFromGray(gray) {
  const dct = dct2(gray, 32)
  const low = []
  for (let y = 0; y < 8; y++) {
    for (let x = 0; x < 8; x++) low.push(dct[y * 32 + x])
  }
  const sorted = [...low].sort((a, b) => a - b)
  const med = (sorted[31] + sorted[32]) / 2
  let hex = ""
  for (let i = 0; i < 64; i += 4) {
    let nibble = 0
    for (let b = 0; b < 4; b++) if (low[i + b] > med) nibble |= 1 << (3 - b)
    hex += nibble.toString(16)
  }
  return hex
}

function gray32(bitmap) {
  const image = resize(bitmap, 32)
  const gray = new Float64Array(32 * 32)
  for (let i = 0; i < 32 * 32; i++) {
    const o = i * 4
    gray[i] = image.data[o] * 0.299 + image.data[o + 1] * 0.587 + image.data[o + 2] * 0.114
  }
  return gray
}

function fitLongEdge(bitmap, maxEdge) {
  const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height))
  const width = Math.max(1, Math.round(bitmap.width * scale))
  const height = Math.max(1, Math.round(bitmap.height * scale))
  const canvas = document.createElement("canvas")
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext("2d", { willReadFrequently: true })
  ctx.imageSmoothingEnabled = true
  ctx.imageSmoothingQuality = "high"
  ctx.drawImage(bitmap, 0, 0, width, height)
  return canvas
}

export function featuresFromBitmap(bitmap) {
  const fitted = fitLongEdge(bitmap, 512)
  return {
    cues: shotCues(resize(fitted, 80)),
    sig: colorSignature(resize(fitted, 48)),
    phash: phashFromGray(gray32(fitted)),
  }
}

function startsNew(previous, current, gap, pause) {
  if (hamming(previous.phash, current.phash) <= 12 && gap <= 180) return false
  const a = previous.cues
  const b = current.cues
  if (!a || !b) return gap >= 8
  const center = dot(a.v, b.v)
  const border = b.border
  const zoomed = a.border - border >= 0.15
  const medium = Math.max(30, pause * 0.44)
  const wide = Math.max(18, pause * 0.29)
  if (gap < 40 && border < 0.15 && (center >= 0.7 || gap >= 18)) return false
  if (medium <= gap && gap < pause && border < 0.12 && center >= 0.45) return false
  if (gap >= pause && center < 0.93) return true
  if (gap >= medium && center < 0.9) return true
  if (gap >= wide && border >= 0.51 && center < 0.9) return true
  if (gap >= 8 && gap < 18 && border < 0.15 && center < 0.65 && !zoomed) return true

  const ps = previous.sig
  const cs = current.sig
  if (!ps || !cs) return false
  const cosine = dot(ps.v, cs.v)
  const rgb = l2(ps.mean, cs.mean)
  const blueDelta = Math.abs(ps.blue - cs.blue)
  if (gap >= 10 && gap < 70 && blueDelta >= 0.35 && Math.min(ps.blue, cs.blue) < 0.15 && cosine < 0.85) {
    return true
  }
  if (gap >= 10 && gap < 20 && cosine < 0.74 && rgb >= 0.1) return true
  if (gap >= 20 && gap < 70 && (cosine < 0.7 || (cosine < 0.78 && rgb >= 0.09))) return true
  if (cosine < 0.55 && gap >= 10) return true
  return false
}

function matchesEarlier(group, item) {
  const cues = item.cues
  if (!cues || cues.border >= 0.35) return false
  for (const member of group) {
    if (member.cues && dot(member.cues.v, cues.v) >= 0.86) return true
  }
  return false
}

function linkScore(previous, current) {
  const distance = hamming(previous.phash, current.phash)
  const cosine = previous.cues && current.cues ? dot(previous.cues.v, current.cues.v) : 0
  if (distance <= 12) return 1 + cosine
  return cosine
}

function splitToCap(group) {
  if (group.length <= MAX_PHOTOS) return [group]
  let cut = 0
  let best = Infinity
  for (let i = 0; i < group.length - 1; i++) {
    const score = linkScore(group[i], group[i + 1])
    if (score < best) {
      best = score
      cut = i
    }
  }
  return splitToCap(group.slice(0, cut + 1)).concat(splitToCap(group.slice(cut + 1)))
}

function attachLeading(groups) {
  const kept = []
  let index = 0
  while (index < groups.length) {
    const group = groups[index]
    if (group.length === 1 && index + 1 < groups.length) {
      const cues = group[0].cues
      const current = group[0].time
      const next = groups[index + 1][0].time
      const gap = current == null || next == null ? 999 : Math.abs(next - current) / 1000
      const nextCues = groups[index + 1][0].cues
      if (cues && nextCues && cues.border < 0.25 && nextCues.border >= 0.5 && gap <= 120) {
        groups[index + 1] = group.concat(groups[index + 1])
        index++
        continue
      }
    }
    kept.push(group)
    index++
  }
  return kept
}

function clusterTimed(items, pause) {
  const ordered = [...items].sort((a, b) => (a.time ?? 0) - (b.time ?? 0) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id))
  const groups = [[ordered[0]]]
  let previousTime = ordered[0].time
  for (let i = 1; i < ordered.length; i++) {
    const item = ordered[i]
    const gap = previousTime == null || item.time == null ? pause : Math.abs(item.time - previousTime) / 1000
    if (startsNew(groups[groups.length - 1].at(-1), item, gap, pause)) {
      if (gap <= 40 && matchesEarlier(groups[groups.length - 1], item)) groups[groups.length - 1].push(item)
      else groups.push([item])
    } else {
      groups[groups.length - 1].push(item)
    }
    if (item.time != null) previousTime = item.time
  }
  return attachLeading(groups).flatMap(splitToCap)
}

export function clusterItems(items, pause = PAUSE_SECONDS) {
  if (!items.length) return []
  if (items.length === 1) return [[items[0]]]
  const timed = []
  const untimed = []
  for (const item of items) {
    const time = parseTime(item.time)
    const copy = { ...item, time }
    if (time == null) untimed.push(copy)
    else timed.push(copy)
  }
  const clusters = []
  if (timed.length >= 2) clusters.push(...clusterTimed(timed, pause))
  else if (timed.length === 1) clusters.push(timed)
  if (untimed.length >= 2) {
    untimed.sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id))
    const base = Date.UTC(2000, 0, 1)
    untimed.forEach((item, index) => {
      item.time = base + 12000 * index
    })
    clusters.push(...clusterTimed(untimed, pause))
  } else if (untimed.length === 1) clusters.push(untimed)
  for (const cluster of clusters) {
    cluster.sort((a, b) => (a.time ?? 0) - (b.time ?? 0) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id))
  }
  clusters.sort((a, b) => (a[0].time ?? 8e15) - (b[0].time ?? 8e15) || a[0].id.localeCompare(b[0].id))
  return clusters
}
