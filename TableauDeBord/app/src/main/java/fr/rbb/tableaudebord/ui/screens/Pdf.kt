package fr.rbb.tableaudebord.ui.screens

import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.pdf.PdfRenderer
import android.os.ParcelFileDescriptor
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.File

/** Aperçu PDF avec le moteur d'Android (PdfRenderer), sans bibliothèque externe. */
@Composable
fun EcranPdf(fichier: File) {
    val ouvert = remember(fichier) {
        runCatching {
            val pfd = ParcelFileDescriptor.open(fichier, ParcelFileDescriptor.MODE_READ_ONLY)
            pfd to PdfRenderer(pfd)
        }.getOrNull()
    }
    val verrou = remember { Mutex() }
    DisposableEffect(ouvert) {
        onDispose {
            ouvert?.let { (pfd, r) ->
                runCatching { r.close() }
                runCatching { pfd.close() }
            }
        }
    }
    if (ouvert == null) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text("Ce PDF ne peut pas être affiché ici.")
        }
        return
    }
    val rendu = ouvert.second
    LazyColumn(
        Modifier.fillMaxSize().background(MaterialTheme.colorScheme.surfaceVariant),
        contentPadding = PaddingValues(8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        items(rendu.pageCount) { index ->
            PagePdf(rendu, index, verrou)
        }
    }
}

@Composable
private fun PagePdf(rendu: PdfRenderer, index: Int, verrou: Mutex) {
    var image by remember(index) { mutableStateOf<Bitmap?>(null) }
    var ratio by remember(index) { mutableStateOf(0.707f) }
    LaunchedEffect(rendu, index) {
        image = withContext(Dispatchers.IO) {
            // PdfRenderer n'accepte qu'une page ouverte à la fois.
            verrou.withLock {
                runCatching {
                    rendu.openPage(index).use { page ->
                        ratio = page.width.toFloat() / page.height.toFloat()
                        val largeur = 1400
                        val hauteur = (largeur / ratio).toInt()
                        val bmp = Bitmap.createBitmap(largeur, hauteur, Bitmap.Config.ARGB_8888)
                        bmp.eraseColor(Color.WHITE)
                        page.render(bmp, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                        bmp
                    }
                }.getOrNull()
            }
        }
    }
    Box(Modifier.fillMaxWidth().aspectRatio(ratio).background(androidx.compose.ui.graphics.Color.White), contentAlignment = Alignment.Center) {
        val img = image
        if (img == null) {
            CircularProgressIndicator()
        } else {
            Image(img.asImageBitmap(), contentDescription = "Page ${index + 1}", contentScale = ContentScale.FillWidth, modifier = Modifier.fillMaxWidth())
        }
    }
}
