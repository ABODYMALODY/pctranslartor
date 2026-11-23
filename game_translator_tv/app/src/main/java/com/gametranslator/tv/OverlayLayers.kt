package com.gametranslator.tv

import android.content.Context
import android.graphics.PixelFormat
import android.os.Build
import android.provider.Settings
import android.util.DisplayMetrics
import android.view.Gravity
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.ImageView
import kotlin.math.roundToInt

/**
 * A transparent window over everything on the TV, including the HDMI input picture.
 * Each "group" (subtitles, instructions, phone, menu) is a set of pictures; a new message for a
 * group replaces the old pictures of that group.
 */
class OverlayLayers(private val context: Context) {

    private val windowManager = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private val root = FrameLayout(context)
    private val groups = HashMap<String, List<ImageView>>()
    var attached = false
        private set

    /** Returns false while the "Display over other apps" permission is missing. */
    fun attach(): Boolean {
        if (attached) return true
        if (!Settings.canDrawOverlays(context)) return false
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS or
                WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED,
            PixelFormat.TRANSLUCENT,
        )
        params.gravity = Gravity.TOP or Gravity.START
        return try {
            windowManager.addView(root, params)
            attached = true
            true
        } catch (e: Exception) {
            false
        }
    }

    fun detach() {
        if (!attached) return
        try {
            windowManager.removeView(root)
        } catch (_: Exception) {
        }
        attached = false
    }

    fun setGroup(id: String, layers: List<Layer>) {
        groups.remove(id)?.forEach { root.removeView(it) }
        if (layers.isEmpty()) return
        val (screenW, screenH) = screenSize()
        val views = layers.map { layer ->
            ImageView(context).apply {
                scaleType = ImageView.ScaleType.FIT_XY
                setImageBitmap(layer.bitmap)
                layoutParams = FrameLayout.LayoutParams(
                    (layer.w * screenW).roundToInt().coerceAtLeast(1),
                    (layer.h * screenH).roundToInt().coerceAtLeast(1),
                ).apply {
                    leftMargin = (layer.x * screenW).roundToInt()
                    topMargin = (layer.y * screenH).roundToInt()
                }
            }
        }
        views.forEach { root.addView(it) }
        groups[id] = views
    }

    fun clearAll() {
        root.removeAllViews()
        groups.clear()
    }

    private fun screenSize(): Pair<Int, Int> {
        if (root.width > 0 && root.height > 0) return root.width to root.height
        return if (Build.VERSION.SDK_INT >= 30) {
            val bounds = windowManager.currentWindowMetrics.bounds
            bounds.width() to bounds.height()
        } else {
            val metrics = DisplayMetrics()
            @Suppress("DEPRECATION")
            windowManager.defaultDisplay.getRealMetrics(metrics)
            metrics.widthPixels to metrics.heightPixels
        }
    }
}
