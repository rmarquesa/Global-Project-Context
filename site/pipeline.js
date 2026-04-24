// Scroll-triggered pipeline animation for the Index → Embed → Store → Retrieve
// section. Activates stages and moves a "packet" along the flow once the
// section enters the viewport. Pure DOM/CSS transitions, no libs.

const STAGE_DWELL = 900;    // ms a packet "sits" inside each stage
const STAGE_TRANSITION = 450; // ms between stages

function animatePipeline(pipeline) {
  const stages = [...pipeline.querySelectorAll(".pipelineStage")];
  const packet = pipeline.querySelector(".pipelinePacket");
  if (!stages.length) return;

  let stepIdx = 0;

  function gotoStage(i) {
    stages.forEach((s, idx) => {
      s.classList.toggle("is-active", idx === i);
      s.classList.toggle("is-done", idx < i);
    });
    if (!packet) return;
    const target = stages[i];
    const pr = pipeline.getBoundingClientRect();
    const tr = target.getBoundingClientRect();
    // Packet positioned relative to pipeline
    const x = tr.left - pr.left + tr.width / 2;
    const y = tr.top - pr.top + tr.height / 2;
    packet.style.transform = `translate(${x}px, ${y}px)`;
    packet.style.opacity = "1";
  }

  function tick() {
    if (stepIdx >= stages.length) {
      // Reset after a pause
      setTimeout(() => {
        stages.forEach((s) => s.classList.remove("is-active", "is-done"));
        if (packet) packet.style.opacity = "0";
        stepIdx = 0;
        setTimeout(tick, 1200);
      }, 2400);
      return;
    }
    gotoStage(stepIdx);
    stepIdx++;
    setTimeout(tick, STAGE_DWELL + STAGE_TRANSITION);
  }

  let started = false;
  const io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting && !started) {
          started = true;
          setTimeout(tick, 400);
        }
      }
    },
    { threshold: 0.35 },
  );
  io.observe(pipeline);
}

window.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-pipeline]").forEach(animatePipeline);
});
