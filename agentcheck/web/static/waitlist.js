/* The hosted cards are a waitlist, so every one of them has to DO something.
   Plain script, no framework: this page is the only marketing surface, and it
   should not need a build step to collect an address. */
(function () {
  var form = document.getElementById('waitlist-form');
  var email = document.getElementById('wl-email');
  var status = document.getElementById('wl-status');
  var submit = document.getElementById('wl-submit');
  var plan = 'unsure';

  function open(which) {
    plan = which;
    form.hidden = false;
    var label = document.querySelector('.waitlist label');
    if (label) {
      label.textContent = 'Email, and we will tell you when ' +
        (which === 'free' ? 'the free hosted tier' : which + '') + ' opens';
    }
    email.focus();
  }

  document.querySelectorAll('[data-waitlist]').forEach(function (b) {
    b.addEventListener('click', function () { open(b.dataset.waitlist); });
  });
  document.getElementById('wl-cancel').addEventListener('click', function () {
    form.hidden = true; status.textContent = '';
  });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var v = (email.value || '').trim();
    if (!v || v.indexOf('@') < 0) {
      status.textContent = 'That does not look like an email address.';
      return;
    }
    submit.disabled = true;
    status.textContent = 'Sending…';
    fetch('/v1/waitlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: v, plan: plan })
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        submit.disabled = false;
        if (!res.ok) {
          status.textContent = (res.d && res.d.detail) || 'Could not save that; try again.';
          return;
        }
        status.textContent = res.d.already_on_list
          ? 'You are already on the list — we will be in touch.'
          : 'On the list. Meanwhile, self-host is free and works today.';
        email.value = '';
        count();
      })
      .catch(function () {
        submit.disabled = false;
        status.textContent = 'Could not reach the server; try again.';
      });
  });

  // Social proof only once it exists. An empty "0 waiting" is worse than none.
  function count() {
    fetch('/v1/waitlist/count').then(function (r) { return r.json(); })
      .then(function (d) {
        var el = document.getElementById('wl-count');
        if (d && d.n > 2) {
          el.hidden = false;
          el.textContent = d.n + ' teams are waiting for hosted sign-up.';
        }
      }).catch(function () { /* the page still reads fine without it */ });
  }
  count();
})();
