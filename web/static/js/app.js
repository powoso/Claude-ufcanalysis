/* Minimal interactivity for UFC Analyzer */
document.addEventListener('DOMContentLoaded', function() {
    // Animate stat values on scroll
    const observer = new IntersectionObserver(function(entries) {
        entries.forEach(function(entry) {
            if (entry.isIntersecting) {
                entry.target.classList.add('visible');
            }
        });
    }, { threshold: 0.1 });

    document.querySelectorAll('.stat-card, .fight-card').forEach(function(el) {
        observer.observe(el);
    });
});
