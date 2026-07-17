// CSP-safe static asset (script-src 'self'). Keyboard shortcuts for fast validation.
// Not unit-tested (static asset); behavior is exercised by the E2E task.
(function () {
  var form = document.querySelector('.validate-form');
  if (!form) return;
  var card = document.querySelector('.validate-card');
  var cardId = card ? card.getAttribute('data-card-id') : null;

  function selectCandidate(n) {
    var radio = form.querySelector('input[name="card_ref_id"][data-index="' + n + '"]');
    if (radio) radio.checked = true;
  }
  function postTo(action) {
    var f = document.createElement('form');
    f.method = 'post';
    f.action = action;
    document.body.appendChild(f);
    f.submit();
  }

  document.addEventListener('keydown', function (e) {
    if (e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName) && e.key !== 'Enter') return;
    switch (e.key) {
      case '1': selectCandidate(1); break;
      case '2': selectCandidate(2); break;
      case '3': selectCandidate(3); break;
      case 'Enter': e.preventDefault(); form.submit(); break;
      case 's': if (cardId) postTo('/cards/' + cardId + '/skip'); break;
      case 'n': if (cardId) postTo('/cards/' + cardId + '/not-card'); break;
    }
  });

  // Clicking a search hit fills the form's card_ref_id via a hidden radio and submits.
  document.addEventListener('click', function (e) {
    var hit = e.target.closest ? e.target.closest('.search-hit') : null;
    if (!hit) return;
    var refId = hit.getAttribute('data-ref-id');
    var hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.name = 'card_ref_id';
    hidden.value = refId;
    form.appendChild(hidden);
    form.submit();
  });
})();
