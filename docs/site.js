// Mobile menu toggle. That's all the JavaScript this site needs.
document.getElementById("menu-btn").addEventListener("click", function () {
  document.querySelector("nav.side").classList.toggle("open");
});
