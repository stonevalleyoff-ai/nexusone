package fr.rbb.tableaudebord.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import kotlinx.coroutines.flow.Flow
import net.zetetic.database.sqlcipher.SupportOpenHelperFactory
import java.security.KeyStore
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Dernière synthèse reçue pour une boîte (le JSON brut, tel que Claude l'a écrit). */
@Entity(tableName = "synthese")
data class SyntheseEntite(
    @PrimaryKey val boite: String,
    val nomFichier: String,
    val json: String,
    val recueLe: Long,
)

/** Ce que tu as fait d'une tâche, renvoyé à Claude dans retours.json. */
@Entity(tableName = "etat_tache")
data class EtatTacheEntite(
    @PrimaryKey val id: String,
    /** "fait", "ignore", "reporte" ou "note". */
    val statut: String,
    val nouvelleEcheance: String?,
    val note: String?,
    val le: Long,
)

/** Question posée à Claude depuis l'appli. */
@Entity(tableName = "question")
data class QuestionEntite(
    @PrimaryKey val id: String,
    val boite: String?,
    val texte: String,
    val le: Long,
)

@Dao
interface TdbDao {
    @Query("SELECT * FROM synthese")
    fun syntheses(): Flow<List<SyntheseEntite>>

    @Query("SELECT * FROM synthese")
    suspend fun synthesesMaintenant(): List<SyntheseEntite>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun enregistrerSynthese(s: SyntheseEntite)

    @Query("SELECT * FROM etat_tache")
    fun etats(): Flow<List<EtatTacheEntite>>

    @Query("SELECT * FROM etat_tache WHERE le >= :depuis")
    suspend fun etatsDepuis(depuis: Long): List<EtatTacheEntite>

    @Query("SELECT * FROM etat_tache")
    suspend fun etatsMaintenant(): List<EtatTacheEntite>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun enregistrerEtat(e: EtatTacheEntite)

    @Query("DELETE FROM etat_tache WHERE id = :id")
    suspend fun effacerEtat(id: String)

    @Query("SELECT * FROM question ORDER BY le DESC")
    fun questions(): Flow<List<QuestionEntite>>

    @Query("SELECT * FROM question WHERE le >= :depuis")
    suspend fun questionsDepuis(depuis: Long): List<QuestionEntite>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun enregistrerQuestion(q: QuestionEntite)

    @Query("DELETE FROM etat_tache WHERE le < :avant")
    suspend fun purgerEtats(avant: Long)

    @Query("DELETE FROM question WHERE le < :avant")
    suspend fun purgerQuestions(avant: Long)
}

@Database(
    entities = [SyntheseEntite::class, EtatTacheEntite::class, QuestionEntite::class],
    version = 1,
    exportSchema = true,
)
abstract class TdbBase : RoomDatabase() {
    abstract fun dao(): TdbDao

    companion object {
        fun ouvrir(context: Context): TdbBase {
            System.loadLibrary("sqlcipher")
            val cle = CleBase.obtenir(context)
            return Room.databaseBuilder(context, TdbBase::class.java, "tableau-de-bord.db")
                .openHelperFactory(SupportOpenHelperFactory(cle))
                .fallbackToDestructiveMigration()
                .build()
        }
    }
}

/**
 * Clé de chiffrement de la base : 32 octets aléatoires, eux-mêmes chiffrés par une clé
 * AES du coffre matériel Android (Keystore). La clé Keystore ne sort jamais du téléphone.
 */
object CleBase {
    private const val ALIAS = "tdb_cle_base"
    private const val PREFS = "tdb_cle"
    private const val CHAMP = "cle_chiffree"

    fun obtenir(context: Context): ByteArray {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val stockee = prefs.getString(CHAMP, null)
        if (stockee != null) {
            val brut = Base64.decode(stockee, Base64.NO_WRAP)
            val iv = brut.copyOfRange(0, 12)
            val chiffre = brut.copyOfRange(12, brut.size)
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.DECRYPT_MODE, cleKeystore(), GCMParameterSpec(128, iv))
            return c.doFinal(chiffre)
        }
        val nouvelle = ByteArray(32).also { SecureRandom().nextBytes(it) }
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, cleKeystore())
        val tout = c.iv + c.doFinal(nouvelle)
        prefs.edit().putString(CHAMP, Base64.encodeToString(tout, Base64.NO_WRAP)).apply()
        return nouvelle
    }

    private fun cleKeystore(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return gen.generateKey()
    }
}
