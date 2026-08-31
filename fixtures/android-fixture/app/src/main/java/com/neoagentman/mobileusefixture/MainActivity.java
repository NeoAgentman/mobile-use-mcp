package com.neoagentman.mobileusefixture;

import android.app.Activity;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.Locale;

/**
 * Small, dependency-free Android surface used by the public stdio acceptance flow.
 *
 * <p>The activity intentionally uses platform views rather than AndroidX or an OEM widget. Its
 * resource IDs and visible labels are part of the fixture contract; keep them stable when adding
 * new acceptance scenarios.</p>
 */
public final class MainActivity extends Activity {
    private static final long DELAYED_ELEMENT_DELAY_MS = 1250L;
    private static final int ROW_COUNT = 32;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private LinearLayout root;
    private TextView status;
    private TextView revision;
    private TextView changedValue;
    private Button delayedTrigger;
    private TextView delayedElement;
    private EditText input;
    private ScrollView scroll;
    private TextView scrollState;
    private boolean changed;
    private int fixtureRevision;

    private final Runnable revealDelayedElement = new Runnable() {
        @Override
        public void run() {
            delayedElement.setVisibility(View.VISIBLE);
            delayedTrigger.setEnabled(true);
            bumpRevision();
            status.setText(R.string.status_delayed);
            delayedElement.setText(R.string.delayed_ready);
            delayedElement.setContentDescription(getString(R.string.delayed_ready));
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Window window = getWindow();
        window.setStatusBarColor(getColor(R.color.fixture_background));
        window.setNavigationBarColor(getColor(R.color.fixture_background));
        buildLayout();
        resetFixtureState();
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(revealDelayedElement);
        super.onDestroy();
    }

    private void buildLayout() {
        root = new LinearLayout(this);
        root.setId(R.id.fixture_root);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.TOP);
        root.setBackgroundColor(getColor(R.color.fixture_background));
        root.setContentDescription("Android MCP fixture root");

        TextView title = makeTextView(R.id.fixture_title, R.string.fixture_title, 24);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        title.setTextColor(getColor(R.color.fixture_primary_dark));
        title.setContentDescription(getString(R.string.fixture_title));
        addFixed(root, title, 58);

        TextView subtitle = makeTextView(View.NO_ID, R.string.fixture_subtitle, 14);
        subtitle.setTextColor(getColor(R.color.fixture_muted));
        addFixed(root, subtitle, 34);

        status = makeTextView(R.id.fixture_status, R.string.status_ready, 14);
        status.setTextColor(getColor(R.color.fixture_text));
        addFixed(root, status, 34);

        revision = makeTextView(R.id.fixture_revision, R.string.status_ready, 12);
        revision.setTextColor(getColor(R.color.fixture_muted));
        addFixed(root, revision, 26);

        Button changeButton = makeButton(
                R.id.fixture_change_button,
                R.string.change_button,
                "Trigger deterministic UI change"
        );
        changeButton.setOnClickListener(view -> toggleChangedState());
        addFixed(root, changeButton, 52);

        changedValue = makeTextView(
                R.id.fixture_change_value,
                R.string.change_value_baseline,
                14
        );
        changedValue.setTextColor(getColor(R.color.fixture_primary));
        changedValue.setContentDescription(getString(R.string.change_value_baseline));
        addFixed(root, changedValue, 30);

        delayedTrigger = makeButton(
                R.id.fixture_delayed_trigger,
                R.string.delayed_trigger,
                "Reveal delayed element"
        );
        delayedTrigger.setOnClickListener(view -> scheduleDelayedElement());
        addFixed(root, delayedTrigger, 52);

        delayedElement = makeTextView(
                R.id.fixture_delayed_element,
                R.string.delayed_ready,
                14
        );
        delayedElement.setTextColor(getColor(R.color.fixture_primary));
        delayedElement.setBackgroundColor(getColor(R.color.fixture_accent));
        delayedElement.setPadding(dp(12), 0, dp(12), 0);
        delayedElement.setContentDescription(getString(R.string.delayed_ready));
        addFixed(root, delayedElement, 36);
        delayedElement.setVisibility(View.GONE);

        input = new EditText(this);
        input.setId(R.id.fixture_text_input);
        input.setHint(R.string.input_hint);
        input.setContentDescription("Unicode input field");
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
        input.setImeOptions(EditorInfo.IME_ACTION_DONE);
        input.setTextColor(getColor(R.color.fixture_text));
        input.setHintTextColor(getColor(R.color.fixture_muted));
        input.setBackgroundColor(getColor(R.color.fixture_panel));
        input.setPadding(dp(12), 0, dp(12), 0);
        addFixed(root, input, 54);

        Button resetButton = makeButton(
                R.id.fixture_reset_button,
                R.string.reset_button,
                "Reset Android MCP fixture state"
        );
        resetButton.setOnClickListener(view -> resetFixtureState());
        addFixed(root, resetButton, 52);

        scrollState = makeTextView(R.id.fixture_scroll_state, R.string.scroll_top, 12);
        scrollState.setTextColor(getColor(R.color.fixture_muted));
        addFixed(root, scrollState, 26);

        scroll = new ScrollView(this);
        scroll.setId(R.id.fixture_scroll_container);
        scroll.setFillViewport(true);
        scroll.setContentDescription("Scrollable fixture content");
        scroll.setBackgroundColor(getColor(R.color.fixture_panel));
        scroll.setOnScrollChangeListener((view, scrollX, scrollY, oldScrollX, oldScrollY) -> {
            if (scrollY > 0) {
                scrollState.setText(R.string.scroll_moved);
                scrollState.setContentDescription(getString(R.string.scroll_moved));
            } else {
                scrollState.setText(R.string.scroll_top);
                scrollState.setContentDescription(getString(R.string.scroll_top));
            }
        });

        LinearLayout rows = new LinearLayout(this);
        rows.setOrientation(LinearLayout.VERTICAL);
        rows.setPadding(dp(12), dp(6), dp(12), dp(24));
        for (int index = 1; index <= ROW_COUNT; index++) {
            TextView row = makeTextView(
                    View.NO_ID,
                    "Fixture row " + String.format(Locale.ROOT, "%02d", index),
                    15
            );
            row.setTextColor(getColor(R.color.fixture_text));
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.setContentDescription("Fixture scroll row " + index);
            rows.addView(row, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    dp(48)
            ));
        }
        scroll.addView(rows, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT
        ));
        root.addView(scroll, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1
        ));

        setContentView(root);
    }

    private TextView makeTextView(int id, int textResource, int textSizeSp) {
        return makeTextView(id, getString(textResource), textSizeSp);
    }

    private TextView makeTextView(int id, String text, int textSizeSp) {
        TextView view = new TextView(this);
        if (id != View.NO_ID) {
            view.setId(id);
        }
        view.setText(text);
        view.setTextSize(textSizeSp);
        view.setGravity(Gravity.CENTER_VERTICAL);
        view.setPadding(dp(12), 0, dp(12), 0);
        view.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES);
        return view;
    }

    private Button makeButton(int id, int textResource, String contentDescription) {
        Button button = new Button(this);
        button.setId(id);
        button.setText(textResource);
        button.setAllCaps(false);
        button.setContentDescription(contentDescription);
        button.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES);
        return button;
    }

    private void addFixed(LinearLayout parent, View child, int heightDp) {
        parent.addView(child, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(heightDp)
        ));
    }

    private void toggleChangedState() {
        changed = !changed;
        bumpRevision();
        int value = changed ? R.string.change_value_changed : R.string.change_value_baseline;
        changedValue.setText(value);
        changedValue.setContentDescription(getString(value));
        status.setText(changed ? R.string.status_changed : R.string.status_ready);
    }

    private void scheduleDelayedElement() {
        handler.removeCallbacks(revealDelayedElement);
        delayedTrigger.setEnabled(false);
        delayedElement.setVisibility(View.GONE);
        delayedElement.setText(R.string.delayed_pending);
        delayedElement.setContentDescription(getString(R.string.delayed_pending));
        bumpRevision();
        status.setText(R.string.status_pending);
        handler.postDelayed(revealDelayedElement, DELAYED_ELEMENT_DELAY_MS);
    }

    private void resetFixtureState() {
        handler.removeCallbacks(revealDelayedElement);
        changed = false;
        fixtureRevision = 0;
        if (changedValue != null) {
            changedValue.setText(R.string.change_value_baseline);
            changedValue.setContentDescription(getString(R.string.change_value_baseline));
        }
        if (delayedTrigger != null) {
            delayedTrigger.setEnabled(true);
        }
        if (delayedElement != null) {
            delayedElement.setVisibility(View.GONE);
            delayedElement.setText(R.string.delayed_ready);
            delayedElement.setContentDescription(getString(R.string.delayed_ready));
        }
        if (input != null) {
            input.setText("");
        }
        if (scroll != null) {
            scroll.setScrollY(0);
        }
        if (scrollState != null) {
            scrollState.setText(R.string.scroll_top);
            scrollState.setContentDescription(getString(R.string.scroll_top));
        }
        if (status != null) {
            status.setText(R.string.status_reset);
        }
        renderRevision();
    }

    private void bumpRevision() {
        fixtureRevision++;
        renderRevision();
    }

    private void renderRevision() {
        if (revision != null) {
            revision.setText("Fixture revision: " + fixtureRevision);
        }
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
