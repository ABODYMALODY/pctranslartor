package com.gametranslator.tv

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/** The screen you see when you open the app on the TV: the pairing code and the connection state. */
class MainActivity : Activity() {

    private lateinit var codeView: TextView
    private lateinit var statusView: TextView
    private lateinit var addressView: TextView
    private lateinit var hintView: TextView
    private lateinit var permissionButton: Button
    private lateinit var disconnectButton: Button

    private val teal = Color.parseColor("#2DD4BF")
    private val muted = Color.parseColor("#9AA3B2")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildUi())
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    override fun onResume() {
        super.onResume()
        OverlayService.statusListener = { show(it) }
        startForegroundService(Intent(this, OverlayService::class.java))
        OverlayService.instance?.let { show(it.status()) }
    }

    override fun onPause() {
        OverlayService.statusListener = null
        super.onPause()
    }

    // ------------------------------------------------------------------ UI

    private fun show(status: OverlayService.Status) {
        codeView.text = status.code.chunked(3).joinToString(" ")
        addressView.text = "TV address: ${status.address}"
        if (status.connectedTo != null) {
            statusView.text = "●  Connected to ${status.connectedTo}"
            statusView.setTextColor(teal)
            hintView.text = "You can go back to the PlayStation now. Translations will appear over the game."
            disconnectButton.visibility = View.VISIBLE
        } else {
            statusView.text = "Waiting for your PC…"
            statusView.setTextColor(muted)
            hintView.text = "On the PC: Game Translator → Show on → Connect a TV, then type this code."
            disconnectButton.visibility = View.GONE
        }
        permissionButton.visibility = if (status.canDraw) View.GONE else View.VISIBLE
        if (!status.canDraw) {
            hintView.text = "First allow Game Translator to draw over other apps, so it can show translations over the game."
        }
    }

    private fun openOverlaySettings() {
        val attempts = listOf(
            Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")),
            Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION),
        )
        for (intent in attempts) {
            try {
                startActivity(intent)
                return
            } catch (_: Exception) {
            }
        }
        hintView.text = "This TV has no settings page for it. From a PC with ADB run:\n" +
            "adb shell appops set $packageName SYSTEM_ALERT_WINDOW allow"
    }

    private fun buildUi(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.parseColor("#0F1115"))
            setPadding(dp(48), dp(32), dp(48), dp(32))
        }
        root.addView(text("GAME TRANSLATOR", 30f, Color.WHITE, bold = true))
        root.addView(View(this).apply {
            setBackgroundColor(teal)
            layoutParams = LinearLayout.LayoutParams(dp(360), dp(3)).apply { setMargins(0, dp(12), 0, dp(28)) }
        })
        root.addView(text("Connection code", 18f, muted))
        codeView = text("------", 64f, Color.WHITE, bold = true).apply { letterSpacing = 0.15f }
        root.addView(codeView)
        statusView = text("", 22f, muted).apply { setPadding(0, dp(16), 0, dp(8)) }
        root.addView(statusView)
        hintView = text("", 17f, muted).apply { setPadding(0, 0, 0, dp(20)) }
        root.addView(hintView)

        permissionButton = button("Allow drawing over the TV picture") { openOverlaySettings() }
        root.addView(permissionButton)
        disconnectButton = button("Disconnect") { OverlayService.instance?.disconnect() }
        root.addView(disconnectButton)
        addressView = text("", 14f, muted).apply { setPadding(0, dp(24), 0, 0) }
        root.addView(addressView)
        return root
    }

    private fun text(value: String, sizeSp: Float, color: Int, bold: Boolean = false) = TextView(this).apply {
        text = value
        setTextColor(color)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, sizeSp)
        gravity = Gravity.CENTER
        if (bold) typeface = Typeface.DEFAULT_BOLD
    }

    private fun button(label: String, onClick: () -> Unit) = Button(this).apply {
        text = label
        isAllCaps = false
        setTextColor(Color.parseColor("#062A26"))
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 18f)
        background = GradientDrawable().apply {
            setCornerRadius(dp(28).toFloat())
            setColor(teal)
        }
        setPadding(dp(32), dp(12), dp(32), dp(12))
        // The remote's focus must be visible on a TV.
        setOnFocusChangeListener { v, hasFocus ->
            v.scaleX = if (hasFocus) 1.06f else 1f
            v.scaleY = if (hasFocus) 1.06f else 1f
            v.alpha = if (hasFocus) 1f else 0.8f
        }
        setOnClickListener { onClick() }
        layoutParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT
        ).apply { setMargins(0, dp(8), 0, dp(8)) }
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()
}
