// CSP forbids inline scripts (script-src 'self'); this is a same-origin static file.
// Native EventSource handles reconnect; each (re)connect re-sends the full snapshot,
// which supersedes any missed events — no Last-Event-ID replay needed (SSE contract).
(function () {
  var root = document.querySelector('.progress');
  if (!root) return;
  var batchId = root.getAttribute('data-batch-id');
  var es = new EventSource('/batches/' + batchId + '/events');

  function setText(id, v) { var el = document.getElementById(id); if (el) el.textContent = String(v); }

  function applySnapshot(s) {
    setText('photos-done', s.photos_done);
    setText('cards-total', s.cards_total);
    setText('cards-identified', s.cards_identified);
    setText('cards-validation', s.cards_validation);
    setText('cards-unreadable', s.cards_unreadable);
    var vlink = document.getElementById('validate-link');
    if (vlink) vlink.style.display = s.cards_validation > 0 ? '' : 'none';
    var ticker = document.getElementById('ticker');
    if (ticker) {
      ticker.innerHTML = '';
      (s.ticker || []).forEach(function (t) {
        var li = document.createElement('li');
        li.setAttribute('data-card-id', t.card_id);
        li.textContent = (t.name || 'unknown') + ' — ' + t.confidence + '% (' + t.status + ')';
        ticker.appendChild(li);
      });
    }
  }

  es.addEventListener('snapshot', function (ev) { applySnapshot(JSON.parse(ev.data)); });

  es.addEventListener('progress', function (ev) {
    var d = JSON.parse(ev.data);
    if (d.event === 'card_identified' && d.card) {
      var ticker = document.getElementById('ticker');
      if (ticker) {
        var li = document.createElement('li');
        li.setAttribute('data-card-id', d.card_id);
        li.textContent = (d.card.name || 'unknown') + ' — ' + d.card.confidence + '% (' + d.card.status + ')';
        ticker.insertBefore(li, ticker.firstChild);
        while (ticker.children.length > 20) ticker.removeChild(ticker.lastChild);
      }
    }
    if (d.event === 'batch_complete') {
      es.close();
      var rlink = document.getElementById('results-link');
      if (rlink) rlink.style.display = '';
    }
  });

  es.onerror = function () { /* EventSource auto-reconnects; snapshot re-syncs state */ };
})();
