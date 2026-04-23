const canvas = document.getElementById("memoryGraph");
const ctx = canvas.getContext("2d");

let width = 0;
let height = 0;
let points = [];
let pointer = { x: 0, y: 0, active: false };

function resize() {
  const ratio = window.devicePixelRatio || 1;
  width = canvas.clientWidth;
  height = canvas.clientHeight;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

  const count = Math.max(48, Math.min(110, Math.floor((width * height) / 15000)));
  points = Array.from({ length: count }, (_, index) => ({
    x: Math.random() * width,
    y: Math.random() * height,
    vx: (Math.random() - 0.5) * 0.35,
    vy: (Math.random() - 0.5) * 0.35,
    r: index % 9 === 0 ? 2.4 : 1.4,
  }));
}

function draw() {
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#101514";
  ctx.fillRect(0, 0, width, height);

  for (const point of points) {
    point.x += point.vx;
    point.y += point.vy;

    if (point.x < 0 || point.x > width) point.vx *= -1;
    if (point.y < 0 || point.y > height) point.vy *= -1;

    if (pointer.active) {
      const dx = pointer.x - point.x;
      const dy = pointer.y - point.y;
      const dist = Math.hypot(dx, dy);
      if (dist < 180) {
        point.x -= dx * 0.0009;
        point.y -= dy * 0.0009;
      }
    }
  }

  for (let i = 0; i < points.length; i += 1) {
    for (let j = i + 1; j < points.length; j += 1) {
      const a = points[i];
      const b = points[j];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      if (dist < 138) {
        const alpha = (1 - dist / 138) * 0.32;
        ctx.strokeStyle = `rgba(64, 178, 146, ${alpha})`;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }
  }

  for (const point of points) {
    ctx.fillStyle = point.r > 2 ? "#e0653f" : "#f7efdf";
    ctx.beginPath();
    ctx.arc(point.x, point.y, point.r, 0, Math.PI * 2);
    ctx.fill();
  }

  requestAnimationFrame(draw);
}

window.addEventListener("resize", resize);
window.addEventListener("pointermove", (event) => {
  pointer = { x: event.clientX, y: event.clientY, active: true };
});
window.addEventListener("pointerleave", () => {
  pointer.active = false;
});

resize();
draw();
