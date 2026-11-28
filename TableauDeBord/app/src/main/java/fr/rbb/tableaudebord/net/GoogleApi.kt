package fr.rbb.tableaudebord.net

import fr.rbb.tableaudebord.model.JsonTdb
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.util.concurrent.TimeUnit

/** Jeton refusé par Google (expiré ou retiré) : il faut redemander l'autorisation. */
class JetonInvalide : Exception("Jeton Google expiré")

class ErreurApi(val code: Int, message: String) : Exception("Erreur Google $code : $message")

@Serializable
data class DriveFichier(
    val id: String,
    val name: String,
    val mimeType: String = "",
    val modifiedTime: String? = null,
    val size: String? = null,
) {
    val estDossier: Boolean get() = mimeType == "application/vnd.google-apps.folder"
    val estPdf: Boolean get() = mimeType == "application/pdf" || name.endsWith(".pdf", ignoreCase = true)
}

@Serializable
private data class DriveListe(
    val files: List<DriveFichier> = emptyList(),
    val nextPageToken: String? = null,
)

@Serializable
data class EvenementAgenda(
    val id: String,
    val summary: String? = null,
    val location: String? = null,
    val description: String? = null,
    val htmlLink: String? = null,
    val start: MomentAgenda? = null,
    val end: MomentAgenda? = null,
)

@Serializable
data class MomentAgenda(
    @SerialName("dateTime") val dateHeure: String? = null,
    val date: String? = null,
)

@Serializable
private data class ListeAgenda(
    val items: List<EvenementAgenda> = emptyList(),
    val nextPageToken: String? = null,
)

/** Appels REST minimaux vers Drive v3 et Calendar v3, sans bibliothèque Google lourde. */
class GoogleApi(private val jeton: () -> String?) {

    private val http = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    private val typeJson = "application/json; charset=UTF-8".toMediaType()

    private fun requete(url: String): Request.Builder {
        val t = jeton() ?: throw JetonInvalide()
        return Request.Builder().url(url).header("Authorization", "Bearer $t")
    }

    private fun executer(req: Request): ByteArray {
        http.newCall(req).execute().use { rep ->
            val corps = rep.body?.bytes() ?: ByteArray(0)
            if (rep.code == 401) throw JetonInvalide()
            if (!rep.isSuccessful) {
                throw ErreurApi(rep.code, String(corps, Charsets.UTF_8).take(300))
            }
            return corps
        }
    }

    private suspend fun getTexte(url: String): String = withContext(Dispatchers.IO) {
        String(executer(requete(url).get().build()), Charsets.UTF_8)
    }

    // ---------- Drive ----------

    /** Liste les fichiers qui répondent à une requête Drive (toutes les pages). */
    suspend fun listerDrive(q: String, ordre: String = "name"): List<DriveFichier> {
        val resultat = mutableListOf<DriveFichier>()
        var page: String? = null
        do {
            val url = "https://www.googleapis.com/drive/v3/files".toHttpUrl().newBuilder()
                .addQueryParameter("q", q)
                .addQueryParameter("fields", "nextPageToken,files(id,name,mimeType,modifiedTime,size)")
                .addQueryParameter("orderBy", ordre)
                .addQueryParameter("pageSize", "200")
                .apply { page?.let { addQueryParameter("pageToken", it) } }
                .build()
                .toString()
            val liste = JsonTdb.decodeFromString(DriveListe.serializer(), getTexte(url))
            resultat += liste.files
            page = liste.nextPageToken
        } while (page != null && resultat.size < 2000)
        // Une page peut se décaler pendant la lecture : pas de doublon (clé de liste unique).
        return resultat.distinctBy { it.id }
    }

    suspend fun enfants(dossierId: String): List<DriveFichier> =
        listerDrive("'$dossierId' in parents and trashed = false", "folder,name desc")

    suspend fun chercher(texte: String): List<DriveFichier> {
        val propre = texte.replace("\\", "").replace("'", "\\'")
        return listerDrive(
            "name contains '$propre' and trashed = false and mimeType != 'application/vnd.google-apps.folder'",
            "modifiedTime desc",
        )
    }

    suspend fun telechargerTexte(fichierId: String): String =
        getTexte("https://www.googleapis.com/drive/v3/files/$fichierId?alt=media")

    suspend fun telechargerVers(fichierId: String, cible: File) = withContext(Dispatchers.IO) {
        val req = requete("https://www.googleapis.com/drive/v3/files/$fichierId?alt=media").get().build()
        http.newCall(req).execute().use { rep ->
            if (rep.code == 401) throw JetonInvalide()
            if (!rep.isSuccessful) throw ErreurApi(rep.code, "téléchargement impossible")
            cible.parentFile?.mkdirs()
            val tmp = File(cible.path + ".part")
            rep.body!!.byteStream().use { entree -> tmp.outputStream().use { entree.copyTo(it) } }
            tmp.renameTo(cible)
        }
    }

    /** Crée un fichier JSON dans un dossier (autorisé par drive.file) et renvoie son id. */
    suspend fun creerJson(dossierId: String, nom: String, contenu: String): String = withContext(Dispatchers.IO) {
        val meta = buildJsonObject {
            put("name", nom)
            put("mimeType", "application/json")
            put("parents", buildJsonArray { add(dossierId) })
        }.toString()
        val corps = MultipartBody.Builder()
            .setType("multipart/related".toMediaType())
            .addPart(meta.toRequestBody(typeJson))
            .addPart(contenu.toRequestBody(typeJson))
            .build()
        val req = requete("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name")
            .post(corps).build()
        val rep = String(executer(req), Charsets.UTF_8)
        JsonTdb.decodeFromString(DriveFichier.serializer(), rep).id
    }

    /** Remplace le contenu d'un fichier créé par l'appli. */
    suspend fun majJson(fichierId: String, contenu: String) = withContext(Dispatchers.IO) {
        val corps: RequestBody = contenu.toRequestBody(typeJson)
        val req = requete("https://www.googleapis.com/upload/drive/v3/files/$fichierId?uploadType=media")
            .patch(corps).build()
        executer(req)
        Unit
    }

    // ---------- Agenda ----------

    suspend fun evenements(debutIso: String, finIso: String): List<EvenementAgenda> {
        val resultat = mutableListOf<EvenementAgenda>()
        var page: String? = null
        do {
            val url = "https://www.googleapis.com/calendar/v3/calendars/primary/events".toHttpUrl().newBuilder()
                .addQueryParameter("timeMin", debutIso)
                .addQueryParameter("timeMax", finIso)
                .addQueryParameter("singleEvents", "true")
                .addQueryParameter("orderBy", "startTime")
                .addQueryParameter("maxResults", "250")
                .apply { page?.let { addQueryParameter("pageToken", it) } }
                .build()
                .toString()
            val liste = JsonTdb.decodeFromString(ListeAgenda.serializer(), getTexte(url))
            resultat += liste.items
            page = liste.nextPageToken
        } while (page != null && resultat.size < 1000)
        return resultat
    }
}
